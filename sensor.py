#!/usr/bin/env python3
"""
Live Seismic Detection Sensor — multi-station consensus + TDOA epicenter localization.

Loads pre-trained StreamingNet ensemble from /checkpoints/,
connects to a SeedLink server, runs inference on each station independently,
fires alert when N_CONSENSUS stations agree within CONSENSUS_WINDOW seconds,
and estimates the epicenter via TDOA least-squares when 3+ stations have arrivals.

All config via environment variables (see .env / fly.toml).

STATIONS format: "GE.APE,GE.MORC,GE.BORG,GE.KBS"  (NET.STA pairs, comma-separated)
"""
import os, time, math, collections, warnings, threading
import numpy as np
import torch, torch.nn as nn, torch.nn.functional as F
warnings.filterwarnings('ignore')

# ── Config from env ────────────────────────────────────────────────────────────
CHECKPOINT_DIR   = os.environ.get('CHECKPOINT_DIR', './checkpoints')
SEEDLINK_SERVER  = os.environ.get('SEEDLINK_SERVER', 'geofon.gfz-potsdam.de:18000')
STATIONS_RAW     = os.environ.get('STATIONS', 'GE.APE,GE.MORC,GE.BORG,GE.KBS')
CHANNELS         = os.environ.get('CHANNELS', 'HHZ,HHN,HHE').split(',')
THRESHOLD        = float(os.environ.get('THRESHOLD', '0.835'))
N_SEEDS          = int(os.environ.get('N_SEEDS', '3'))
ALERT_COOLDOWN   = float(os.environ.get('ALERT_COOLDOWN', '60.0'))
N_CONSENSUS      = int(os.environ.get('N_CONSENSUS', '2'))
CONSENSUS_WINDOW = float(os.environ.get('CONSENSUS_WINDOW', '120.0'))
P_VEL_KM_S      = float(os.environ.get('P_VEL_KM_S', '8.0'))   # teleseismic P-wave speed
LOC_MIN_STA      = int(os.environ.get('LOC_MIN_STA', '3'))       # stations needed for location
P_LEAD_S         = float(os.environ.get('P_LEAD_S', '0.4'))      # model's pre-P horizon

# Parse stations: "GE.APE,GE.MORC" → [('GE','APE'), ('GE','MORC')]
STATIONS = []
for s in STATIONS_RAW.split(','):
    s = s.strip()
    if '.' in s:
        net, sta = s.split('.', 1)
        STATIONS.append((net.strip(), sta.strip()))
    else:
        STATIONS.append(('GE', s))

DEVICE        = 'cpu'
K             = 128
CYCLES        = 1
WIN_SAMPLES   = 100
STRIDE        = 10
TARGET_SRATE  = 100.0
BUF_DECAY     = 0.876
BUF_STRENGTH  = 1.429
MAG_MAX_CREDIBLE = 7.5   # regression head saturates above this; suppress display
MB_DELAY_S    = 15.0     # wait after detection before computing mb (P coda fill)
MB_WIN_S      = 10.0     # P-wave measurement window length for mb

def fmt_mag(mag_est):
    if mag_est > MAG_MAX_CREDIBLE:
        return "---"
    return f"M{max(-2.0, mag_est):.1f}"

# ── Known station coordinates (lat, lon) — fallback if FDSN fetch fails ───────
KNOWN_COORDS = {
    'GE.APE':  (37.069,  25.531),   # Aegean, Greece (FDSN-confirmed)
    'GE.MORC': (49.781,  16.978),   # Morava, Czech Republic
    'GE.BORG': (64.747, -21.328),   # Borgarfjordur, Iceland
    'GE.KBS':  (78.926,  11.943),   # Ny-Ålesund, Svalbard
    'GE.WLF':  (49.664,   6.153),   # Walferdange, Luxembourg
    'GE.STU':  (48.771,   9.194),   # Stuttgart, Germany
    'GE.MAHO': (39.932,   4.267),   # Mahon, Menorca, Spain
    'GE.MTE':  (38.528,  -7.538),   # Mértola, Portugal
    'GE.MATE': (40.649,  16.704),   # Matera, Italy
}

station_coords    = {}   # populated at startup
station_inventory = {}   # key → obspy Inventory with instrument response

def fetch_station_coords():
    """Fetch FDSN coords + instrument response (level=response); fall back gracefully."""
    global station_coords
    try:
        from obspy.clients.fdsn import Client
        client = Client("GEOFON")
        for net, sta in STATIONS:
            key = f"{net}.{sta}"
            try:
                inv = client.get_stations(network=net, station=sta, level="response")
                st = inv[0][0]
                station_coords[key]    = (st.latitude, st.longitude)
                station_inventory[key] = inv
                print(f"  coords {key}: {st.latitude:.3f}°N {st.longitude:.3f}°E (FDSN+response)", flush=True)
            except Exception:
                # Response fetch failed; try coords-only
                try:
                    from obspy.clients.fdsn import Client as C2
                    inv_s = C2("GEOFON").get_stations(network=net, station=sta, level="station")
                    st = inv_s[0][0]
                    station_coords[key] = (st.latitude, st.longitude)
                    print(f"  coords {key}: {st.latitude:.3f}°N {st.longitude:.3f}°E (FDSN, no response)", flush=True)
                except Exception:
                    if key in KNOWN_COORDS:
                        station_coords[key] = KNOWN_COORDS[key]
                        lat, lon = KNOWN_COORDS[key]
                        print(f"  coords {key}: {lat:.3f}°N {lon:.3f}°E (hardcoded)", flush=True)
                    else:
                        print(f"  coords {key}: unknown — will skip in localization", flush=True)
    except Exception:
        for net, sta in STATIONS:
            key = f"{net}.{sta}"
            if key in KNOWN_COORDS:
                station_coords[key] = KNOWN_COORDS[key]

# ── Model ─────────────────────────────────────────────────────────────────────
class ConvBlock(nn.Module):
    def __init__(self, ci, co, k=7):
        super().__init__()
        self.net = nn.Sequential(nn.Conv1d(ci,co,k,padding=k//2), nn.BatchNorm1d(co), nn.ReLU())
    def forward(self, x): return self.net(x)

class StreamingNet(nn.Module):
    def __init__(self, perm_seed=0):
        super().__init__()
        self.enc = nn.Sequential(ConvBlock(3,32), ConvBlock(32,64), ConvBlock(64,K),
                                 nn.AdaptiveAvgPool1d(1))
        perm = torch.tensor(np.random.RandomState(perm_seed).permutation(K), dtype=torch.long)
        self.register_buffer('perm', perm)
        self.cls = nn.Linear(K, 2)
        self.mag = nn.Linear(K, 1)

    def forward(self, x):
        h = self.enc(x).squeeze(-1)
        buf = torch.zeros_like(h)
        for _ in range(CYCLES):
            h = torch.relu(h[:, self.perm])
            buf = BUF_DECAY * buf + (1 - BUF_DECAY) * h.detach()
            h = h + BUF_STRENGTH * buf
        return self.cls(h), self.mag(h).squeeze(-1)

    def predict(self, window_np):
        self.eval()
        with torch.no_grad():
            xb = torch.tensor(window_np[None], dtype=torch.float32)
            logits, mag = self(xb)
        return float(F.softmax(logits, dim=1)[0, 1]), float(mag[0])

# ── Load ensemble ─────────────────────────────────────────────────────────────
def load_ensemble():
    models = []
    for seed in range(N_SEEDS):
        ckpt = os.path.join(CHECKPOINT_DIR, f'seed_{seed}.pt')
        if not os.path.exists(ckpt):
            raise FileNotFoundError(f"Checkpoint not found: {ckpt}")
        m = StreamingNet(perm_seed=seed)
        m.load_state_dict(torch.load(ckpt, map_location='cpu'))
        m.eval()
        models.append(m)
        print(f"  loaded seed {seed} ← {ckpt}", flush=True)
    return models

def ensemble_predict(models, window_np):
    confs, mags = zip(*[m.predict(window_np) for m in models])
    return float(np.mean(confs)), float(np.mean(mags))

def normalize_window(w):
    w = w.copy()
    for i in range(3):
        w[i] /= w[i].std() + 1e-6
    return w

# ── TDOA epicenter localization ────────────────────────────────────────────────
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def locate_epicenter(arrivals):
    """
    arrivals: list of (station_key, p_arrival_unix)
              p_arrival_unix = detection_time + P_LEAD_S (corrected for model horizon)
    Returns (lat, lon, origin_time_unix, rms_s) or None.
    Requires scipy.
    """
    from scipy.optimize import minimize

    # Filter to stations with known coordinates
    obs = [(key, t) for key, t in arrivals if key in station_coords]
    if len(obs) < LOC_MIN_STA:
        return None

    sta_lat  = np.array([station_coords[k][0] for k, _ in obs])
    sta_lon  = np.array([station_coords[k][1] for k, _ in obs])
    arr_time = np.array([t for _, t in obs])

    def cost(params):
        lat0, lon0, t0 = params
        dists = np.array([haversine_km(lat0, lon0, sta_lat[i], sta_lon[i])
                          for i in range(len(obs))])
        pred  = t0 + dists / P_VEL_KM_S
        return float(np.sum((pred - arr_time)**2))

    # Initial guess: station centroid, rough origin time
    lat0 = float(np.mean(sta_lat))
    lon0 = float(np.mean(sta_lon))
    t0   = float(np.min(arr_time)) - 1200.0  # assume 20-min travel for teleseismic

    res = minimize(cost, [lat0, lon0, t0], method='Nelder-Mead',
                   options={'xatol': 0.05, 'fatol': 0.1, 'maxiter': 50000})

    lat_e, lon_e, t0_e = res.x
    n = len(obs)
    rms = math.sqrt(res.fun / n)

    # Clamp to valid range
    lat_e = max(-90.0, min(90.0, lat_e))
    lon_e = ((lon_e + 180) % 360) - 180

    return lat_e, lon_e, t0_e, rms

# ── Body-wave magnitude (mb) — IASPEI/Richter formula ─────────────────────────
def estimate_mb(key, p_arrival_unix, epicenter_latlon):
    """
    mb = log10(A / T) + Q(Δ)
    A   = peak ground displacement (nm) from instrument-corrected HHZ
    T   = dominant period at the peak (s)
    Q   = empirical attenuation correction (Richter 1958, shallow teleseismic P)
    Returns (mb_float, None) or (None, reason_str).
    """
    if key not in station_inventory:
        return None, "no response inventory"
    if key not in station_rings or 'HHZ' not in station_rings[key]:
        return None, "no HHZ ring"
    if epicenter_latlon is None or key not in station_coords:
        return None, "no epicenter/coords"

    ring = station_rings[key]['HHZ']
    data = np.array(list(ring), dtype=np.float32)
    if len(data) < 200:
        return None, "insufficient buffer"

    from obspy import Trace as OTrace, UTCDateTime
    buf_end   = time.time()
    buf_start = buf_end - len(data) / TARGET_SRATE

    tr = OTrace(data=data.copy())
    tr.stats.network       = key.split('.')[0]
    tr.stats.station       = key.split('.')[1]
    tr.stats.channel       = 'HHZ'
    tr.stats.sampling_rate = TARGET_SRATE
    tr.stats.starttime     = UTCDateTime(buf_start)

    try:
        tr.remove_response(inventory=station_inventory[key],
                           output="DISP", water_level=60,
                           pre_filt=(0.01, 0.05, 4.0, 8.0))
    except Exception as e:
        return None, f"response removal: {e}"

    data_nm = tr.data * 1e9  # m → nm

    p_idx  = max(0, int((p_arrival_unix - buf_start) * TARGET_SRATE))
    p_win  = data_nm[p_idx : p_idx + int(MB_WIN_S * TARGET_SRATE)]
    if len(p_win) < 50:
        return None, "P-window outside buffer"

    A = float(np.abs(p_win).max())
    if A <= 0:
        return None, "zero amplitude"

    zc = np.where(np.diff(np.sign(p_win)))[0]
    T  = float(2.0 * np.mean(np.diff(zc)) / TARGET_SRATE) if len(zc) >= 4 else 1.0
    T  = max(0.05, min(5.0, T))

    sta_lat, sta_lon = station_coords[key]
    epi_lat, epi_lon = epicenter_latlon
    dist_deg = haversine_km(sta_lat, sta_lon, epi_lat, epi_lon) / 111.195
    dist_deg = max(2.0, min(100.0, dist_deg))

    # Q(Δ) — Richter (1958) table approximation for shallow focus
    Q = (5.0 + 0.013 * dist_deg) if dist_deg < 20.0 else (5.1 + 0.015 * dist_deg)

    mb = max(0.0, min(10.0, math.log10(A / T) + Q))
    return round(mb, 1), None


def report_mb_deferred(stations_fired, p_arrivals, epicenter_latlon):
    """Thread: waits MB_DELAY_S then measures mb from each station's ring buffer."""
    time.sleep(MB_DELAY_S)
    ts  = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    mbs = []
    for k in sorted(stations_fired):
        p_t = p_arrivals.get(k)
        if p_t is None:
            continue
        mb, err = estimate_mb(k, p_t, epicenter_latlon)
        if mb is not None:
            dist_str = ""
            if epicenter_latlon and k in station_coords:
                d = haversine_km(station_coords[k][0], station_coords[k][1],
                                 epicenter_latlon[0], epicenter_latlon[1]) / 111.195
                dist_str = f"  Δ={d:.1f}°"
            print(f"  [mb {ts}] {k}: mb={mb:.1f}{dist_str}", flush=True)
            mbs.append(mb)
        else:
            print(f"  [mb {ts}] {k}: skipped ({err})", flush=True)

    if not mbs:
        return
    consensus = sorted(mbs)[len(mbs) // 2]
    label = f"({len(mbs)} stations, IASPEI)" if len(mbs) > 1 else "(IASPEI body-wave)"
    print(f"  [mb {ts}] mb={consensus:.1f}  {label}", flush=True)


# ── Multi-station consensus state ─────────────────────────────────────────────
station_rings      = {}   # key → {ch: deque}
station_strides    = {}   # key → int
station_status     = {}   # key → float (last status print time)
station_first_arr  = {}   # key → float or None (first P-arrival timestamp, corrected)

recent_detections  = collections.deque()
last_alert         = [0.0]
suppressed_mag_count       = [0]
suppressed_mag_last_report = [0.0]
SUPPRESSED_REPORT_INTERVAL = 60.0

def station_key(net, sta):
    return f"{net}.{sta}"

def init_station_state():
    for net, sta in STATIONS:
        k = station_key(net, sta)
        station_rings[k]     = {ch: collections.deque(maxlen=2000) for ch in CHANNELS}
        station_strides[k]   = 0
        station_status[k]    = 0.0
        station_first_arr[k] = None

def reset_arrivals():
    for k in station_first_arr:
        station_first_arr[k] = None

def check_consensus(now):
    cutoff = now - CONSENSUS_WINDOW
    pruned = [d for d in recent_detections if d[0] >= cutoff]
    recent_detections.clear()
    recent_detections.extend(pruned)
    stations_fired = set(d[1] for d in recent_detections)
    return len(stations_fired) >= N_CONSENSUS, stations_fired

def on_inference(net, sta, conf, mag_est, now):
    ts  = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    key = station_key(net, sta)

    if conf >= THRESHOLD:
        # Record first P-wave arrival (corrected for model pre-P horizon)
        if station_first_arr[key] is None:
            station_first_arr[key] = now + P_LEAD_S

        recent_detections.append((now, key, conf, mag_est))
        consensus_met, stations_fired = check_consensus(now)

        if consensus_met and now - last_alert[0] > ALERT_COOLDOWN:
            last_alert[0] = now
            recent_detections.clear()

            # Snapshot arrivals before reset (needed for deferred mb thread)
            p_arr_snapshot  = {k: t for k, t in station_first_arr.items() if t is not None}
            epicenter_latlon = None

            station_list = ', '.join(sorted(stations_fired))
            if suppressed_mag_count[0] > 0:
                print(f"  [{ts}] {suppressed_mag_count[0]} event(s) suppressed "
                      f"(saturated magnitude estimate)", flush=True)
                suppressed_mag_count[0] = 0
                suppressed_mag_last_report[0] = now
            print(f"\n{'='*60}", flush=True)
            print(f"  DETECTION  {ts}", flush=True)
            print(f"  Stations:   {station_list}  ({len(stations_fired)}/{N_CONSENSUS} consensus)", flush=True)
            print(f"  Confidence: {conf:.4f}  (threshold={THRESHOLD})", flush=True)
            print(f"  Magnitude:  mb computing... (+{MB_DELAY_S:.0f}s)", flush=True)
            print(f"  Lead time:  +{P_LEAD_S}s before P-arrival", flush=True)

            # Attempt epicenter localization
            arrivals = [(k, t) for k, t in station_first_arr.items() if t is not None]
            if len(arrivals) >= LOC_MIN_STA:
                try:
                    loc = locate_epicenter(arrivals)
                    if loc:
                        lat_e, lon_e, t0_e, rms = loc
                        epicenter_latlon = (lat_e, lon_e)
                        origin_ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0_e))
                        ns = 'N' if lat_e >= 0 else 'S'
                        ew = 'E' if lon_e >= 0 else 'W'
                        sta_used = [k for k, _ in arrivals if k in station_coords]
                        print(f"  Epicenter:  {abs(lat_e):.2f}°{ns} {abs(lon_e):.2f}°{ew}  "
                              f"(rms={rms:.1f}s, {len(sta_used)} stations)", flush=True)
                        print(f"  Origin:     {origin_ts}  (est.)", flush=True)
                    else:
                        n_known = sum(1 for k, _ in arrivals if k in station_coords)
                        print(f"  Epicenter:  need {LOC_MIN_STA} stations w/ coords "
                              f"(have {n_known})", flush=True)
                except Exception as e:
                    print(f"  Epicenter:  localization failed ({e})", flush=True)
            else:
                n_have = len(arrivals)
                print(f"  Epicenter:  need {LOC_MIN_STA}+ stations "
                      f"(have {n_have} P-arrival(s))", flush=True)

            print(f"{'='*60}\n", flush=True)
            reset_arrivals()

            # Launch deferred mb computation in background
            threading.Thread(
                target=report_mb_deferred,
                args=(set(stations_fired), p_arr_snapshot, epicenter_latlon),
                daemon=True,
            ).start()

        elif not consensus_met:
            n_waiting = N_CONSENSUS - len(stations_fired)
            print(f"  [{ts}] {key} CANDIDATE conf={conf:.3f} mag={fmt_mag(mag_est)} "
                  f"(waiting for {n_waiting} more station(s))", flush=True)
    else:
        if now - station_status[key] > 10.0:
            if mag_est > MAG_MAX_CREDIBLE:
                suppressed_mag_count[0] += 1
                if now - suppressed_mag_last_report[0] >= SUPPRESSED_REPORT_INTERVAL:
                    print(f"[{ts}] {suppressed_mag_count[0]} event(s) suppressed "
                          f"(saturated magnitude estimate)", flush=True)
                    suppressed_mag_count[0] = 0
                    suppressed_mag_last_report[0] = now
            else:
                print(f"[{ts}] {key}  conf={conf:.3f}  mag={fmt_mag(mag_est)}", flush=True)
            station_status[key] = now

# ── SeedLink client ────────────────────────────────────────────────────────────
def run_sensor(models):
    from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

    init_station_state()

    class Sensor(EasySeedLinkClient):
        def on_data(self, trace):
            net = trace.stats.network
            sta = trace.stats.station
            ch  = trace.stats.channel
            key = station_key(net, sta)

            if key not in station_rings or ch not in CHANNELS:
                return

            data = trace.data.astype(np.float32)
            sr   = trace.stats.sampling_rate
            if abs(sr - TARGET_SRATE) > 1.0:
                from obspy import Trace as OTrace
                t = OTrace(data=trace.data.copy(), header=trace.stats)
                t.resample(TARGET_SRATE)
                data = t.data.astype(np.float32)

            ring = station_rings[key]
            ring[ch].extend(data)
            station_strides[key] += len(data)

            if min(len(ring[c]) for c in CHANNELS) < WIN_SAMPLES:
                return
            if station_strides[key] < STRIDE:
                return
            station_strides[key] = 0

            window = np.array([
                list(ring[CHANNELS[0]])[-WIN_SAMPLES:],
                list(ring[CHANNELS[1]])[-WIN_SAMPLES:],
                list(ring[CHANNELS[2]])[-WIN_SAMPLES:],
            ], dtype=np.float32)

            conf, mag_est = ensemble_predict(models, normalize_window(window))
            on_inference(net, sta, conf, mag_est, time.time())

    print(f"\nFetching station coordinates...", flush=True)
    fetch_station_coords()

    print(f"\nConnecting to {SEEDLINK_SERVER}...", flush=True)
    station_list = ', '.join(f"{n}.{s}" for n, s in STATIONS)
    print(f"  Stations:  {station_list}", flush=True)
    print(f"  Channels:  {CHANNELS}", flush=True)
    print(f"  Threshold: {THRESHOLD}  |  Consensus: {N_CONSENSUS}/{len(STATIONS)} in {CONSENSUS_WINDOW:.0f}s", flush=True)
    print(f"  Cooldown:  {ALERT_COOLDOWN}s  |  P-vel: {P_VEL_KM_S} km/s", flush=True)
    print(f"  Localize:  {LOC_MIN_STA}+ stations required", flush=True)
    print("Ready. Ctrl+C to stop.\n", flush=True)

    backoff = 5
    while True:
        try:
            client = Sensor(SEEDLINK_SERVER)
            for net, sta in STATIONS:
                for ch in CHANNELS:
                    client.select_stream(net, sta, ch)
            backoff = 5
            client.run()
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
            break
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] Connection error: {e}", flush=True)
            print(f"  Retrying in {backoff}s...", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"Seismic Detection Sensor (multi-station consensus + TDOA localization)", flush=True)
    print(f"  model:   StreamingNet {N_SEEDS}-seed ensemble (H-{P_LEAD_S}s, mean-conf)", flush=True)
    print(f"  device:  {DEVICE}", flush=True)
    print(f"\nLoading checkpoints from {CHECKPOINT_DIR}...", flush=True)
    models = load_ensemble()
    print(f"  {N_SEEDS} models loaded.\n", flush=True)
    run_sensor(models)
