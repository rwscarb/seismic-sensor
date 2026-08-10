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
import os, time, math, collections, warnings, threading, dataclasses, json
import numpy as np
import torch, torch.nn as nn, torch.nn.functional as F
warnings.filterwarnings('ignore')

SERVER_START_TIME = time.time()  # recorded once at process start; used as deploy boundary in UI

# ── Config from env ────────────────────────────────────────────────────────────
CHECKPOINT_DIR   = os.environ.get('CHECKPOINT_DIR', './checkpoints')
SEEDLINK_SERVER  = os.environ.get('SEEDLINK_SERVER', 'geofon.gfz-potsdam.de:18000')
STATIONS_RAW     = os.environ.get('STATIONS', 'GE.APE,GE.MORC,GE.BORG,GE.KBS')
IRIS_SERVER      = os.environ.get('IRIS_SERVER', 'rtserve.iris.washington.edu:18000')
IRIS_STATIONS_RAW = os.environ.get('IRIS_STATIONS', '')  # e.g. "IU.COR,CN.PGC,IU.KDAK"
CHANNELS         = os.environ.get('CHANNELS', 'HHZ,HHN,HHE').split(',')
THRESHOLD        = float(os.environ.get('THRESHOLD', '0.835'))
N_SEEDS          = int(os.environ.get('N_SEEDS', '3'))
ALERT_COOLDOWN   = float(os.environ.get('ALERT_COOLDOWN', '60.0'))
N_CONSENSUS      = int(os.environ.get('N_CONSENSUS', '2'))
CONSENSUS_WINDOW = float(os.environ.get('CONSENSUS_WINDOW', '120.0'))
P_VEL_KM_S      = float(os.environ.get('P_VEL_KM_S', '8.0'))   # teleseismic P-wave speed
LOC_MIN_STA      = int(os.environ.get('LOC_MIN_STA', '3'))       # stations needed for location
P_LEAD_S         = float(os.environ.get('P_LEAD_S', '0.4'))      # model's pre-P horizon
WEB_PORT         = int(os.environ.get('WEB_PORT', '8080'))
TUI_MODE         = os.environ.get('TUI', '').lower() in ('1', 'true', 'yes')
TEMP_SCALE       = float(os.environ.get('TEMP_SCALE', '1.0'))   # temperature for classifier calibration
USGS_MIN_MAG      = float(os.environ.get('USGS_MIN_MAG', '4.0')) # min magnitude for USGS catalog lookup
EMSC_MIN_MAG      = float(os.environ.get('EMSC_MIN_MAG', '2.0')) # min magnitude for EMSC fallback lookup
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')       # optional: post detection alerts to Slack
DETECTIONS_PATH  = os.environ.get('DETECTIONS_PATH', '/tmp/detections.json')

# Parse stations: "GE.APE,GE.MORC" → [('GE','APE'), ('GE','MORC')]
def _parse_stations(raw):
    result = []
    for s in raw.split(','):
        s = s.strip()
        if not s:
            continue
        if '.' in s:
            net, sta = s.split('.', 1)
            result.append((net.strip(), sta.strip()))
        else:
            result.append(('GE', s))
    return result

STATIONS      = _parse_stations(STATIONS_RAW)
IRIS_STATIONS = _parse_stations(IRIS_STATIONS_RAW)
ALL_STATIONS  = STATIONS + IRIS_STATIONS   # full combined list for ring init / coord fetch

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

# ── Shared sensor state (thread-safe; drives web UI + TUI) ────────────────────
@dataclasses.dataclass
class StationSnap:
    conf: float = 0.0
    mag_est: float = 0.0
    last_ts: float = 0.0

@dataclasses.dataclass
class DetectionSnap:
    ts: str = ''
    unix_ts: float = 0.0
    stations: dataclasses.field(default_factory=list) = None
    conf: float = 0.0
    logit_gap: float = 0.0   # raw mean logit gap before temperature scaling
    mb: float = None
    mb_approx: bool = False   # True when epicenter unknown; Q(Δ) assumed at 45°
    mb_local: bool = False    # True when amplitude ratio suggests a local/regional source
    epicenter: tuple = None   # (lat, lon) or None
    teleseismic: bool = False  # True when locator RMS > threshold → distant source, pin unreliable
    usgs: dict = None         # USGS ComCat event if matched
    usgs_checked: bool = False  # True once USGS lookup has completed (match or not)

    def __post_init__(self):
        if self.stations is None:
            self.stations = []

def _save_detections(detections):
    try:
        tmp = DETECTIONS_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump([dataclasses.asdict(d) for d in detections], f)
        os.replace(tmp, DETECTIONS_PATH)
    except Exception as e:
        print(f"[persist] save failed: {e}", flush=True)

def _load_detections():
    try:
        with open(DETECTIONS_PATH) as f:
            rows = json.load(f)
        dets = []
        for r in rows:
            d = DetectionSnap(
                ts=r.get('ts', ''),
                unix_ts=r.get('unix_ts', 0.0),
                stations=r.get('stations', []),
                conf=r.get('conf', 0.0),
                logit_gap=r.get('logit_gap', 0.0),
                mb=r.get('mb'),
                mb_approx=r.get('mb_approx', False),
                mb_local=r.get('mb_local', False),
                epicenter=tuple(r['epicenter']) if r.get('epicenter') else None,
                teleseismic=r.get('teleseismic', False),
                usgs=r.get('usgs'),
                usgs_checked=r.get('usgs_checked', False),
            )
            dets.append(d)
        print(f"[persist] loaded {len(dets)} detections from {DETECTIONS_PATH}", flush=True)
        return dets
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[persist] load failed: {e} — starting fresh", flush=True)
        return []

class SensorState:
    def __init__(self):
        self._lock = threading.Lock()
        self.stations: dict = {}    # key → StationSnap
        self.detections: list = []  # DetectionSnap, oldest first

    def update_station(self, key, conf, mag_est):
        with self._lock:
            self.stations[key] = StationSnap(conf=conf, mag_est=mag_est, last_ts=time.time())

    def add_detection(self, det):
        with self._lock:
            self.detections.append(det)
            if len(self.detections) > 500:
                self.detections.pop(0)
            snap = list(self.detections)
        _save_detections(snap)

    def update_mb(self, ref_unix, mb, approx=False, local=False):
        with self._lock:
            for det in reversed(self.detections):
                if abs(det.unix_ts - ref_unix) < 30:
                    det.mb = mb
                    det.mb_approx = approx
                    det.mb_local = local
                    break
            snap = list(self.detections)
        _save_detections(snap)

    def update_usgs(self, ref_unix, event):
        with self._lock:
            for det in reversed(self.detections):
                if abs(det.unix_ts - ref_unix) < 30:
                    det.usgs = event
                    det.usgs_checked = True
                    break
            snap = list(self.detections)
        _save_detections(snap)

    def to_dict(self):
        with self._lock:
            return {
                'stations': {k: dataclasses.asdict(v) for k, v in self.stations.items()},
                'detections': [
                    {**dataclasses.asdict(d), 'stations': list(d.stations)}
                    for d in self.detections[-30:]
                ],
                'now': time.time(),
                'server_start': SERVER_START_TIME,
            }

sensor_state = SensorState()

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
    'GE.KARP': (35.784,  27.154),   # Karpathos, Greece
    # IRIS network — Pacific Northwest / Cascadia
    'IU.COR':  (44.586, -123.304),  # Corvallis, Oregon
    'CN.PGC':  (48.650, -123.452),  # Saanich, BC (Cascadia forearc)
    'IU.KDAK': (57.783, -152.584),  # Kodiak Island, Alaska
    'IU.COLA': (64.900, -147.850),  # College, Alaska
}

station_coords    = {}   # populated at startup
station_inventory = {}   # key → obspy Inventory with instrument response

def _fetch_coords_from(fdsn_client_name, station_list):
    """Try to fetch FDSN coords + response for a list of (net, sta) pairs."""
    from obspy.clients.fdsn import Client
    try:
        client = Client(fdsn_client_name)
    except Exception as e:
        print(f"  [fdsn] {fdsn_client_name} client init failed: {e}", flush=True)
        client = None
    for net, sta in station_list:
        key = f"{net}.{sta}"
        fetched = False
        if client:
            try:
                inv = client.get_stations(network=net, station=sta, level="response")
                st = inv[0][0]
                station_coords[key]    = (st.latitude, st.longitude)
                station_inventory[key] = inv
                print(f"  coords {key}: {st.latitude:.3f}°N {st.longitude:.3f}°E (FDSN+response)", flush=True)
                fetched = True
            except Exception:
                try:
                    inv_s = client.get_stations(network=net, station=sta, level="station")
                    st = inv_s[0][0]
                    station_coords[key] = (st.latitude, st.longitude)
                    print(f"  coords {key}: {st.latitude:.3f}°N {st.longitude:.3f}°E (FDSN, no response)", flush=True)
                    fetched = True
                except Exception:
                    pass
        if not fetched:
            if key in KNOWN_COORDS:
                station_coords[key] = KNOWN_COORDS[key]
                lat, lon = KNOWN_COORDS[key]
                print(f"  coords {key}: {lat:.3f}°N {lon:.3f}°E (hardcoded)", flush=True)
            else:
                print(f"  coords {key}: unknown — will skip in localization", flush=True)

def fetch_station_coords():
    """Fetch FDSN coords + instrument response; fall back to hardcoded gracefully."""
    global station_coords
    try:
        if STATIONS:
            print(f"  [geofon] fetching {len(STATIONS)} station(s)...", flush=True)
            _fetch_coords_from("GEOFON", STATIONS)
        if IRIS_STATIONS:
            print(f"  [iris] fetching {len(IRIS_STATIONS)} station(s)...", flush=True)
            _fetch_coords_from("IRIS", IRIS_STATIONS)
    except Exception as e:
        print(f"  [fdsn] fetch error: {e} — using hardcoded fallback", flush=True)
        for net, sta in ALL_STATIONS:
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
        gap    = float(logits[0, 1] - logits[0, 0])          # raw logit gap (pre-scaling)
        scaled = logits / TEMP_SCALE
        return float(F.softmax(scaled, dim=1)[0, 1]), float(mag[0]), gap

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
    results = [m.predict(window_np) for m in models]
    confs = [r[0] for r in results]
    mags  = [r[1] for r in results]
    gaps  = [r[2] for r in results]
    return float(np.mean(confs)), float(np.mean(mags)), float(np.mean(gaps))

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

    # Analytically solve for optimal t0 given lat/lon (reduces to 2-D search)
    def cost(params):
        lat0, lon0 = params
        dists = np.array([haversine_km(lat0, lon0, sta_lat[i], sta_lon[i])
                          for i in range(len(obs))])
        travel = dists / P_VEL_KM_S
        t0_opt = float(np.mean(arr_time - travel))
        pred   = t0_opt + travel
        return float(np.sum((pred - arr_time)**2))

    # Initial guess: station centroid
    lat0 = float(np.mean(sta_lat))
    lon0 = float(np.mean(sta_lon))

    res = minimize(cost, [lat0, lon0], method='Nelder-Mead',
                   options={'xatol': 0.05, 'fatol': 0.1, 'maxiter': 50000})

    lat_e, lon_e = res.x
    # Recover t0 analytically from final solution
    dists_final = np.array([haversine_km(lat_e, lon_e, sta_lat[i], sta_lon[i])
                            for i in range(len(obs))])
    t0_e = float(np.mean(arr_time - dists_final / P_VEL_KM_S))
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
                           pre_filt=(0.005, 0.01, 3.0, 5.0))
        # IASPEI mb is defined in the 1 Hz band — bandpass before measuring A and T
        tr.filter('bandpass', freqmin=0.5, freqmax=2.0, corners=4, zerophase=True)
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

    # Measure T from zero crossings; after 1 Hz bandpass T should be ~0.5-2s
    zc = np.where(np.diff(np.sign(p_win)))[0]
    T  = float(2.0 * np.mean(np.diff(zc)) / TARGET_SRATE) if len(zc) >= 4 else 1.0
    T  = max(0.5, min(2.0, T))   # IASPEI: constrain to teleseismic P-wave band

    # Q(Δ) — Richter (1958) table approximation for shallow focus.
    # Note: A above is in nm; GR tables assume µm → subtract log10(1000)=3 from Q constants.
    approx = False
    if epicenter_latlon is not None and key in station_coords:
        sta_lat, sta_lon = station_coords[key]
        epi_lat, epi_lon = epicenter_latlon
        dist_deg = haversine_km(sta_lat, sta_lon, epi_lat, epi_lon) / 111.195
        dist_deg = max(2.0, min(100.0, dist_deg))
    else:
        dist_deg = 45.0   # mid-range teleseismic assumption; ±1 mag unit uncertainty
        approx = True

    Q = (2.0 + 0.013 * dist_deg) if dist_deg < 20.0 else (2.1 + 0.015 * dist_deg)
    mb = max(0.0, min(10.0, math.log10(A / T) + Q))
    print(f"  [mb dbg] {key}: A={A:.1f}nm T={T:.2f}s A/T={A/T:.1f} Q={Q:.2f}({dist_deg:.0f}deg{'~' if approx else ''}) -> mb={mb:.1f}", flush=True)
    return round(mb, 1), ('approx' if approx else None), A


def report_mb_deferred(stations_fired, p_arrivals, epicenter_latlon, det_unix):
    """Thread: waits MB_DELAY_S then measures mb from each station's ring buffer."""
    time.sleep(MB_DELAY_S)
    ts  = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    mbs = []
    for k in sorted(stations_fired):
        p_t = p_arrivals.get(k)
        if p_t is None:
            continue
        mb, tag, amp = estimate_mb(k, p_t, epicenter_latlon)
        if mb is not None:
            dist_str = ""
            if epicenter_latlon and k in station_coords:
                d = haversine_km(station_coords[k][0], station_coords[k][1],
                                 epicenter_latlon[0], epicenter_latlon[1]) / 111.195
                dist_str = f"  Δ={d:.1f}°"
            flag = " [approx Δ=45°]" if tag == 'approx' else ""
            print(f"  [mb {ts}] {k}: mb={mb:.1f}{dist_str}{flag}", flush=True)
            mbs.append((mb, tag, amp))
        else:
            print(f"  [mb {ts}] {k}: skipped ({tag})", flush=True)

    if not mbs:
        return
    mb_vals  = [m for m, _, _ in mbs]
    tags     = [t for _, t, _ in mbs]
    amp_vals = [a for _, _, a in mbs]
    consensus = float(np.median(mb_vals))
    approx    = all(t == 'approx' for t in tags)
    # If no epicenter and stations show large amplitude spread, source is likely local/regional
    amp_ratio = max(amp_vals) / min(amp_vals) if len(amp_vals) > 1 and min(amp_vals) > 0 else 1.0
    local_flag = approx and amp_ratio > 5.0
    n = len(mb_vals)
    label = f"({'~' if approx else ''}{n} stations, IASPEI{', Δ≈45°' if approx else ''}{', likely local amp_ratio=' + f'{amp_ratio:.1f}' if local_flag else ''})"
    print(f"  [mb {ts}] mb={'~' if approx else ''}{consensus:.1f}  {label}", flush=True)
    sensor_state.update_mb(det_unix, consensus, approx=approx, local=local_flag)


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
    for net, sta in ALL_STATIONS:
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

def on_inference(net, sta, conf, mag_est, logit_gap, now):
    ts  = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    key = station_key(net, sta)
    sensor_state.update_station(key, conf, mag_est)

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
            is_teleseismic = False
            arrivals = [(k, t) for k, t in station_first_arr.items() if t is not None]
            if len(arrivals) >= LOC_MIN_STA:
                try:
                    loc = locate_epicenter(arrivals)
                    if loc:
                        lat_e, lon_e, t0_e, rms = loc
                        epicenter_latlon = (lat_e, lon_e)
                        is_teleseismic = rms > 15.0
                        origin_ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0_e))
                        ns = 'N' if lat_e >= 0 else 'S'
                        ew = 'E' if lon_e >= 0 else 'W'
                        sta_used = [k for k, _ in arrivals if k in station_coords]
                        tele_str = '  [TELESEISMIC?]' if is_teleseismic else ''
                        print(f"  Epicenter:  {abs(lat_e):.2f}°{ns} {abs(lon_e):.2f}°{ew}  "
                              f"(rms={rms:.1f}s, {len(sta_used)} stations){tele_str}", flush=True)
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

            det_rec = DetectionSnap(
                ts=ts, unix_ts=now,
                stations=sorted(stations_fired),
                conf=conf,
                logit_gap=logit_gap,
                epicenter=epicenter_latlon,
                teleseismic=is_teleseismic if epicenter_latlon else False,
            )
            sensor_state.add_detection(det_rec)

            threading.Thread(
                target=send_slack_alert,
                args=(ts, stations_fired, conf, epicenter_latlon),
                daemon=True,
            ).start()

            reset_arrivals()

            # Launch deferred mb + USGS lookups in background
            threading.Thread(
                target=report_mb_deferred,
                args=(set(stations_fired), p_arr_snapshot, epicenter_latlon, now),
                daemon=True,
            ).start()
            threading.Thread(
                target=report_usgs_deferred,
                args=(now, dict(p_arr_snapshot)),
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

# ── USGS ComCat validation ─────────────────────────────────────────────────────
def query_usgs_event(det_unix, p_arrivals):
    """
    Search USGS ComCat for earthquakes that could explain this detection.
    Searches the window [earliest_P - 2400s, earliest_P - 30s] (up to 40 min before P).
    Returns a dict with mag/place/time or None.
    """
    import urllib.request
    min_arr = min(p_arrivals.values()) if p_arrivals else det_unix
    t0 = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(min_arr - 2400))
    t1 = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(min_arr - 30))
    url = (
        f'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
        f'&starttime={t0}&endtime={t1}'
        f'&minmagnitude={USGS_MIN_MAG}&orderby=magnitude-desc&limit=5'
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        feats = data.get('features', [])
        if not feats:
            return None
        f = feats[0]   # highest magnitude in window
        coords = f['geometry']['coordinates']
        p = f['properties']
        return {
            'mag':      p.get('mag'),
            'magType':  p.get('magType', '?'),
            'place':    p.get('place', '?'),
            'time':     p['time'] / 1000,
            'lat':      coords[1],
            'lon':      coords[0],
            'depth':    coords[2],
            'event_id': f.get('id', ''),
        }
    except Exception:
        return None


def send_slack_alert(ts, stations_fired, conf, epicenter=None):
    """POST a detection alert to Slack webhook if configured."""
    if not SLACK_WEBHOOK_URL:
        return
    import urllib.request
    sta_list = ' · '.join(sorted(stations_fired))
    epi_str  = f'\nEpicenter: `{epicenter[0]:.2f}N {epicenter[1]:.2f}E`' if epicenter else ''
    text = (
        f'🌍 *Seismic Detection*\n'
        f'Time: `{ts}`\n'
        f'Stations: `{sta_list}`\n'
        f'Confidence: `{conf:.3f}`{epi_str}\n'
        f'<https://seismic-sensor.fly.dev|View dashboard>'
    )
    payload = json.dumps({'text': text, 'mrkdwn': True}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  [slack] webhook failed: {e}", flush=True)


def query_emsc_event(det_unix, p_arrivals):
    """
    EMSC fallback: European-Mediterranean catalog, lower magnitude threshold.
    Bounding box covers Europe + Mediterranean to reduce irrelevant global matches.
    """
    import urllib.request
    min_arr = min(p_arrivals.values()) if p_arrivals else det_unix
    t0 = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(min_arr - 2400))
    t1 = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(min_arr - 30))
    url = (
        f'https://www.seismicportal.eu/fdsnws/event/1/query?format=json'
        f'&starttime={t0}&endtime={t1}'
        f'&minmagnitude={EMSC_MIN_MAG}&orderby=magnitude-desc&limit=5'
        f'&minlatitude=25&maxlatitude=75&minlongitude=-30&maxlongitude=60'
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        feats = data.get('features', [])
        if not feats:
            return None
        f = feats[0]
        coords = f['geometry']['coordinates']
        p = f['properties']
        return {
            'mag':      p.get('mag') or p.get('magnitude'),
            'magType':  p.get('magtype') or p.get('magnitudetype', '?'),
            'place':    p.get('flynn_region') or p.get('region', '?'),
            'time':     p['time'] / 1000 if isinstance(p.get('time'), (int, float)) else 0,
            'lat':      coords[1],
            'lon':      coords[0],
            'depth':    coords[2] if len(coords) > 2 else 0,
            'source':   'emsc',
            'event_id': p.get('unid') or f.get('id', ''),
        }
    except Exception:
        return None


def report_usgs_deferred(det_unix, p_arrivals):
    """Thread: queries USGS ~10s after detection, then EMSC if no match."""
    time.sleep(10)
    event = query_usgs_event(det_unix, p_arrivals)
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    if event:
        event['source'] = 'usgs'
        ns = 'N' if event['lat'] >= 0 else 'S'
        ew = 'E' if event['lon'] >= 0 else 'W'
        print(f"  [usgs {ts}] M{event['mag']}{event['magType']} — {event['place']}", flush=True)
        print(f"  [usgs {ts}] {abs(event['lat']):.2f}°{ns} {abs(event['lon']):.2f}°{ew}  "
              f"depth={event['depth']:.0f}km", flush=True)
        sensor_state.update_usgs(det_unix, event)
        return
    print(f"  [usgs {ts}] no USGS match (M{USGS_MIN_MAG}+ in window) — trying EMSC", flush=True)
    event = query_emsc_event(det_unix, p_arrivals)
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    if event:
        ns = 'N' if event['lat'] >= 0 else 'S'
        ew = 'E' if event['lon'] >= 0 else 'W'
        print(f"  [emsc {ts}] M{event['mag']}{event['magType']} — {event['place']}", flush=True)
        print(f"  [emsc {ts}] {abs(event['lat']):.2f}°{ns} {abs(event['lon']):.2f}°{ew}  "
              f"depth={event['depth']:.0f}km", flush=True)
        sensor_state.update_usgs(det_unix, event)
    else:
        print(f"  [emsc {ts}] no EMSC match (M{EMSC_MIN_MAG}+ European window)", flush=True)
        sensor_state.update_usgs(det_unix, None)


# ── Web UI ────────────────────────────────────────────────────────────────────
_WEB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seismic Sensor — %(app_title)s</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:'Courier New',monospace;font-size:13px}
header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:15px;color:#58a6ff;letter-spacing:1px}
#cfg{color:#6e7681;font-size:11px}
[title]{cursor:help}
#status-dot{width:8px;height:8px;border-radius:50%;background:#238636;animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#last-update{color:#6e7681;font-size:11px;margin-left:auto}
#last-event-summary{font-size:11px;color:#8b949e;border-left:1px solid #30363d;padding-left:12px}
.grid{display:grid;grid-template-columns:320px 1fr;gap:12px;padding:12px}
.panel{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px;min-width:0}
.panel-hdr{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px}
.panel-hdr h2{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}
.det-count{font-size:10px;color:#6e7681;background:#21262d;border-radius:8px;padding:1px 6px}
.station{padding:6px 0;border-bottom:1px solid #21262d}
.station:last-child{border-bottom:none}
.sta-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px}
.sta-name{color:#58a6ff;font-weight:bold}
.sta-conf{font-size:11px}
.conf-bar{height:4px;border-radius:2px;background:#21262d;margin-top:2px}
.conf-fill{height:100%;border-radius:2px;transition:width .5s}
.det{display:flex;align-items:center;gap:4px;padding:5px 4px 5px 4px;border-bottom:1px solid #21262d;font-size:11px;min-height:26px;min-width:0;overflow:hidden}
.det:last-child{border-bottom:none}
.det-selected{background:#0d2a15!important;box-shadow:inset 2px 0 0 #3fb950}
.det-time{color:#8b949e;font-size:10px;white-space:nowrap;flex-shrink:0;font-variant-numeric:tabular-nums}
.det-stas{color:#58a6ff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.det-chips-inline{display:flex;gap:3px;flex-shrink:0}
.det-usgs-icon{flex-shrink:0;font-size:13px;line-height:1;cursor:default}.det-usgs-icon[href]{cursor:pointer}
.det-age{color:#6e7681;font-size:10px;white-space:nowrap;flex-shrink:0;text-align:right;min-width:28px}
.det-deploy-sep{display:flex;align-items:center;gap:6px;padding:5px 0;color:#d29922;font-size:10px;letter-spacing:.5px}
.det-deploy-sep::before,.det-deploy-sep::after{content:'';flex:1;border-top:1px solid #2a1f00}
.chip{font-size:10px;border-radius:3px;padding:1px 5px;font-weight:bold;white-space:nowrap}
.chip-mb-low{color:#3fb950;background:#0d2a15}
.chip-mb-mid{color:#d29922;background:#2a1f00}
.chip-mb-high{color:#f85149;background:#2d1216}
.chip-mb-approx{opacity:.8;font-style:italic}
.chip-epi{color:#d29922;background:#2a1f00}
.chip-usgs{color:#a371f7;background:#1e1129}
.chip-emsc{color:#39c5cf;background:#0d1f21}
#map{height:320px;border-radius:4px;margin-top:10px}
.right-col{display:flex;flex-direction:column;gap:12px}
.no-data{color:#6e7681;font-style:italic;font-size:11px}
/* fullscreen map */
#map-wrap{position:relative}
#fs-btn{position:absolute;top:6px;right:6px;z-index:1000;background:#161b22cc;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer;backdrop-filter:blur(4px)}
#fs-btn:hover{color:#e6edf3;border-color:#58a6ff}
body.fs-mode .grid{display:flex}
body.fs-mode .right-col{position:fixed!important;top:50px;right:10px;bottom:10px;width:330px;z-index:1001;background:#161b22bb;backdrop-filter:blur(10px);border:1px solid #30363d;border-radius:8px;overflow:hidden;display:flex!important;flex-direction:column}
body.fs-mode .right-col>.panel{border:none;border-radius:0;background:transparent;flex:1;overflow-y:auto;overflow-x:hidden}
body.fs-mode #left-panel{position:fixed;inset:0;z-index:500;border-radius:0;padding:0;display:flex;flex-direction:column;border:none}
body.fs-mode #left-panel>.panel-hdr,body.fs-mode #stations{display:none}
body.fs-mode #map-wrap{flex:1;margin:0}
body.fs-mode #map{height:100%!important;border-radius:0}
body.fs-mode header{z-index:502;position:relative}
/* fullscreen overlay */
#fs-overlay{display:none;position:fixed;top:55px;left:10px;z-index:1001;background:#161b22cc;border:1px solid #30363d;border-radius:6px;padding:10px 14px;min-width:220px;max-width:280px;backdrop-filter:blur(6px);font-size:11px;pointer-events:none}
body.fs-mode #fs-overlay{display:block}
#fs-overlay h3{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.fso-sta{display:flex;justify-content:space-between;margin-bottom:4px;align-items:center}
.fso-bar{height:3px;border-radius:2px;background:#21262d;margin-top:2px;margin-bottom:6px}
.fso-bar-fill{height:100%;border-radius:2px}
.fso-det{margin-top:8px;border-top:1px solid #21262d;padding-top:8px}
</style>
</head>
<body>
<header>
  <div id="status-dot"></div>
  <h1>&#127757; Seismic Sensor</h1>
  <span id="cfg" title="SeedLink: %(seedlink)s">%(cfg_text)s</span>
  <span id="last-event-summary"></span>
  <span id="last-update">connecting...</span>
  <button id="mute-btn" title="Toggle audio alerts" style="background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;margin-left:4px">&#128266; on</button>
  <select id="tz-sel" title="Display timezone" style="background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:2px 5px;font-size:11px;cursor:pointer;margin-left:6px">
    <option value="auto">Auto TZ</option>
    <option value="UTC">UTC</option>
    <option value="America/Los_Angeles">Pacific</option>
    <option value="America/Denver">Mountain</option>
    <option value="America/Chicago">Central</option>
    <option value="America/New_York">Eastern</option>
    <option value="Europe/London">London</option>
    <option value="Europe/Paris">Paris/Berlin</option>
    <option value="Europe/Helsinki">Helsinki/Athens</option>
    <option value="Europe/Moscow">Moscow</option>
    <option value="Asia/Dubai">Dubai</option>
    <option value="Asia/Kolkata">India</option>
    <option value="Asia/Bangkok">Bangkok</option>
    <option value="Asia/Tokyo">Tokyo</option>
    <option value="Australia/Sydney">Sydney</option>
  </select>
</header>
<div class="grid">
  <div class="panel" id="left-panel">
    <div class="panel-hdr"><h2>Stations</h2></div>
    <div id="stations"></div>
    <div id="map-wrap">
      <button id="fs-btn" title="Toggle fullscreen map">&#x26F6;</button>
      <div id="map"></div>
      <div id="fs-overlay">
        <h3>Stations</h3>
        <div id="fso-stations"></div>
        <div id="fso-det" class="fso-det"></div>
      </div>
    </div>
  </div>
  <div class="right-col">
    <div class="panel" style="flex:1;overflow:auto">
      <div class="panel-hdr"><h2>Detections</h2><span id="det-count" class="det-count"></span><button id="filter-btn" title="Show confirmed catalog matches only" style="margin-left:auto;background:#161b22;border:1px solid #30363d;color:#6e7681;border-radius:4px;padding:2px 7px;font-size:10px;cursor:pointer">✓ confirmed</button></div>
      <div id="detections"></div>
      <div id="det-more" style="display:none;padding:6px 0;text-align:center"><button onclick="showMoreDets()" style="background:#21262d;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:3px 10px;font-size:10px;cursor:pointer">show older</button></div>
    </div>
  </div>
</div>
<script>
const map = L.map('map', {zoomControl:false}).setView([45,10],2);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {attribution:'&copy; OSM &copy; CARTO',subdomains:'abcd',maxZoom:19}).addTo(map);
const staMarkers={}, detMarkers=[];
let lastFlyTs=null, selectedDetTs=null, _pulseIv=null, _pulsePhase=0;
let filterConfirmed=false, detDisplayLimit=100;
function showMoreDets(){detDisplayLimit+=50;}
(()=>{
  const btn=document.getElementById('filter-btn');
  if(!btn)return;
  btn.addEventListener('click',()=>{
    filterConfirmed=!filterConfirmed;
    detDisplayLimit=100;
    btn.style.color=filterConfirmed?'#3fb950':'#6e7681';
    btn.style.borderColor=filterConfirmed?'#3fb950':'#30363d';
  });
})();
function confColor(c){return c>=0.835?'#3fb950':c>=0.5?'#d29922':'#6e7681'}
function fmtAge(ts){const s=Math.round(Date.now()/1000-ts);return s<60?s+'s':s<3600?Math.round(s/60)+'m':Math.round(s/3600)+'h'}
const _browserTz=Intl.DateTimeFormat().resolvedOptions().timeZone;
let _userTz=localStorage.getItem('tz')||'auto';
function _activeTz(){return _userTz==='auto'?_browserTz:_userTz;}
function _tzAbbr(){
  try{return new Intl.DateTimeFormat('en',{timeZone:_activeTz(),timeZoneName:'short'}).formatToParts(new Date()).find(p=>p.type==='timeZoneName').value;}
  catch(e){return _activeTz();}
}
function fmtLocal(isoStr){
  const d=new Date(isoStr);
  const tz=_activeTz();
  const timeStr=d.toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:tz});
  const today=new Date().toLocaleDateString('en',{timeZone:tz});
  const detDay=d.toLocaleDateString('en',{timeZone:tz});
  const prefix=today===detDay?'':d.toLocaleDateString('en',{timeZone:tz,month:'short',day:'numeric'})+' ';
  return `${prefix}${timeStr} ${_tzAbbr()}`;
}
// Timezone selector
(()=>{
  const sel=document.getElementById('tz-sel');
  if(!sel)return;
  sel.value=_userTz;
  if(!sel.value)sel.value='auto';
  sel.addEventListener('change',()=>{_userTz=sel.value;localStorage.setItem('tz',_userTz);});
})();
// fullscreen toggle
document.getElementById('fs-btn').addEventListener('click',()=>{
  document.body.classList.toggle('fs-mode');
  setTimeout(()=>map.invalidateSize(),100);
});
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.body.classList.remove('fs-mode');});
function applyMarkerSelection(){
  if(_pulseIv){clearInterval(_pulseIv);_pulseIv=null;}
  detMarkers.forEach(({m,ts,r})=>{
    if(ts===selectedDetTs){
      m.setStyle({color:'#3fb950',fillColor:'#3fb950',fillOpacity:.9});
      _pulsePhase=0;
      _pulseIv=setInterval(()=>{
        _pulsePhase=(_pulsePhase+0.15)%(2*Math.PI);
        const p=Math.abs(Math.sin(_pulsePhase));
        m.setRadius(r+p*5);
        m.setStyle({fillOpacity:.55+p*.4});
      },40);
    } else {
      m.setRadius(r);
      m.setStyle({color:'#f85149',fillColor:'#f85149',fillOpacity:selectedDetTs?0.25:0.6});
    }
  });
}
function flyToEpi(lat,lon,ts){
  selectedDetTs=ts||null;
  applyMarkerSelection();
  map.getPane('overlayPane').style.visibility='hidden';
  map.flyTo([lat,lon],6,{duration:2.5});
  map.once('moveend',()=>{map.getPane('overlayPane').style.visibility='';applyMarkerSelection();});
}
// audio alert
let audioEnabled=true;
let lastDetTs=null;
let audioCtx=null;
const muteBtn=document.getElementById('mute-btn');
muteBtn.addEventListener('click',()=>{
  audioEnabled=!audioEnabled;
  muteBtn.textContent=audioEnabled?'🔔 on':'🔕 off';
  muteBtn.style.color=audioEnabled?'#8b949e':'#6e7681';
  if(!audioCtx)audioCtx=new(window.AudioContext||window.webkitAudioContext)();
});
function playDetectionAlert(){
  if(!audioEnabled)return;
  try{
    if(!audioCtx)audioCtx=new(window.AudioContext||window.webkitAudioContext)();
    if(audioCtx.state==='suspended')audioCtx.resume();
    [[880,0],[660,0.18],[440,0.32]].forEach(([freq,t])=>{
      const osc=audioCtx.createOscillator();
      const gain=audioCtx.createGain();
      osc.connect(gain);gain.connect(audioCtx.destination);
      osc.frequency.value=freq;osc.type='sine';
      gain.gain.setValueAtTime(0.25,audioCtx.currentTime+t);
      gain.gain.exponentialRampToValueAtTime(0.001,audioCtx.currentTime+t+0.16);
      osc.start(audioCtx.currentTime+t);
      osc.stop(audioCtx.currentTime+t+0.18);
    });
  }catch(e){}
}
// browser desktop notification
if('Notification' in window && Notification.permission==='default'){
  Notification.requestPermission();
}
function showDesktopNotification(det){
  if(!audioEnabled)return;
  if(!('Notification' in window)||Notification.permission!=='granted')return;
  const mbStr=det.mb!=null?(det.mb_local?'local':det.mb_approx?`mb~${det.mb.toFixed(1)}`:`mb=${det.mb.toFixed(1)}`):'mb computing';
  new Notification('🌍 Seismic Detection',{
    body:`${det.stations.join(' · ')} | ${mbStr}`,
    tag:'seismic-det',
    renotify:true,
    silent:true,
  });
}
function update(){
  fetch('/api/state').then(r=>r.json()).then(d=>{
    document.getElementById('last-update').textContent='updated '+new Date().toLocaleTimeString();
    // stations
    const sDiv=document.getElementById('stations');
    sDiv.innerHTML='';
    const sCoords=%(station_coords_json)s;
    Object.entries(d.stations).sort((a,b)=>b[1].conf-a[1].conf).forEach(([k,s])=>{
      const pct=Math.round(s.conf*100);
      const col=confColor(s.conf);
      const coord=sCoords[k]?`${sCoords[k][0].toFixed(2)}°N ${sCoords[k][1].toFixed(2)}°E`:'coords unknown';
      const cardTitle=`${k}\n${coord}\nconf: ${s.conf.toFixed(4)}\nlast sample: ${fmtLocal(new Date(s.last_ts*1000).toISOString())}`;
      const barTitle=`threshold: %(threshold)s | current: ${s.conf.toFixed(3)}`;
      sDiv.innerHTML+=`<div class="station" title="${cardTitle}">
        <div class="sta-row"><span class="sta-name">${k}</span>
        <span class="sta-conf" style="color:${col}">${s.conf.toFixed(3)}</span></div>
        <div class="conf-bar" title="${barTitle}"><div class="conf-fill" style="width:${pct}%;background:${col}"></div></div>
        <div style="color:#6e7681;font-size:10px">${coord} &mdash; ${fmtAge(s.last_ts)} ago</div>
      </div>`;
      if(sCoords[k] && !staMarkers[k]){
        const [lat,lon]=sCoords[k];
        staMarkers[k]=L.circleMarker([lat,lon],{radius:6,color:'#58a6ff',fillColor:'#58a6ff',fillOpacity:.9})
          .bindTooltip(`<b>${k}</b><br>${coord}`,{permanent:false,direction:'top'}).addTo(map);
      }
      if(staMarkers[k]){
        const mc=confColor(s.conf);
        staMarkers[k].setStyle({color:mc,fillColor:mc});
        staMarkers[k].setTooltipContent(`<b>${k}</b><br>${coord}<br>conf: ${s.conf.toFixed(3)}`);
      }
    });
    // detections
    const dDiv=document.getElementById('detections');
    const dets=[...d.detections].reverse();
    const filteredDets=filterConfirmed?dets.filter(det=>det.usgs):dets;
    const cntEl=document.getElementById('det-count');
    if(cntEl)cntEl.textContent=filterConfirmed
      ?`${filteredDets.length} confirmed`
      :(d.detections.length?`${d.detections.length} total`:'');
    // alert on new detection
    if(dets.length){
      const newest=dets[0];
      if(lastDetTs!==null && newest.ts!==lastDetTs){
        playDetectionAlert();
        showDesktopNotification(newest);
      }
      lastDetTs=newest.ts;
    }
    // last-event summary in header
    const sumEl=document.getElementById('last-event-summary');
    if(sumEl&&dets.length){
      const ld=dets[0];
      const mbStr=ld.mb!=null?(ld.mb_local?'local':ld.mb_approx?`mb~${ld.mb.toFixed(1)}`:`mb=${ld.mb.toFixed(1)}`):'mb…';
      sumEl.textContent=`Last: ${mbStr} · ${fmtAge(ld.unix_ts)} ago`;
    }
    if(!dets.length){dDiv.innerHTML='<div class="no-data">No detections yet</div>';return}
    const escAttr=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
    const mbChipClass=mb=>mb>=5?'chip-mb-high':mb>=4?'chip-mb-mid':'chip-mb-low';
    const serverStart=d.server_start||0;
    const deployLabel=(()=>{const dt=new Date(serverStart*1000);return dt.toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',hour12:false,timeZone:_activeTz()})+' '+_tzAbbr();})();
    let sepInserted=false;
    const moreEl=document.getElementById('det-more');
    if(moreEl)moreEl.style.display=filteredDets.length>detDisplayLimit?'block':'none';
    const newHtml=filteredDets.slice(0,detDisplayLimit).map(det=>{
      let sep='';
      if(!sepInserted && det.unix_ts < serverStart){
        sepInserted=true;
        sep=`<div class="det-deploy-sep" title="Process restarted / new version deployed at ${fmtLocal(new Date(serverStart*1000).toISOString())}">deployed ${deployLabel}</div>`;
      }
      // time — local time in card, full UTC in tooltip
      const tPart=fmtLocal(det.ts);
      // mb chip (center)
      let mbChip='';
      if(det.mb!=null){
        if(det.mb_local){
          mbChip=`<span class="chip chip-mb-approx" title="Amplitude ratio between stations suggests a local/regional source; IASPEI mb unreliable">local</span>`;
        } else {
          const lbl=(det.mb_approx?'mb~':'mb=')+det.mb.toFixed(1);
          const cls=mbChipClass(det.mb)+(det.mb_approx?' chip-mb-approx':'');
          mbChip=`<span class="chip ${cls}" title="${det.mb_approx?'approx, assumed distance 45 deg':'IASPEI body-wave'}">${lbl}</span>`;
        }
      } else {
        mbChip=`<span class="chip" style="color:#6e7681;background:#161b22">mb…</span>`;
      }
      // epicenter chip — clickable to fly map to location
      let epiChip='';
      if(det.epicenter){
        if(det.teleseismic){
          epiChip=`<span class="chip chip-epi" title="Localization unreliable (high residual) — likely distant teleseismic source" style="opacity:.7">&#x1F310; teleseismic</span>`;
        } else {
          const [la,lo]=det.epicenter;
          const ns=la>=0?'N':'S', ew=lo>=0?'E':'W';
          epiChip=`<button class="chip chip-epi" onclick="event.stopPropagation();flyToEpi(${la},${lo},'${det.ts}')" title="Fly map to epicenter" style="cursor:pointer;border:none;font-family:inherit">&#x1F4CD; ${Math.abs(la).toFixed(1)}°${ns} ${Math.abs(lo).toFixed(1)}°${ew}</button>`;
        }
      }
      // catalog icon — right-aligned checkmark/cross
      let usgsIcon='';
      let usgsTitle='';
      if(det.usgs){
        const place=det.usgs.place||'';
        const mt=det.usgs.magType||'';
        const src=det.usgs.source||'usgs';
        const srcLabel=src==='emsc'?'EMSC':'USGS';
        const iconColor=src==='emsc'?'#39c5cf':'#a371f7';
        usgsTitle=`${srcLabel}: M${det.usgs.mag}${mt} — ${place}`;
        const eid=det.usgs.event_id||'';
        const href=eid?(src==='emsc'
          ?`https://www.seismicportal.eu/eventdetails.html?unid=${encodeURIComponent(eid)}`
          :`https://earthquake.usgs.gov/earthquakes/eventpage/${encodeURIComponent(eid)}/executive`):'';
        const inner=`<span style="color:${iconColor}">&#10003;</span>`;
        usgsIcon=href
          ?`<a class="det-usgs-icon" href="${href}" target="_blank" rel="noopener" title="${escAttr(usgsTitle)}" style="text-decoration:none" onclick="event.stopPropagation()">${inner}</a>`
          :`<span class="det-usgs-icon" title="${escAttr(usgsTitle)}">${inner}</span>`;
      } else if(det.usgs_checked){
        usgsTitle=`No match in USGS (M%(usgs_min_mag)s+) or EMSC (M%(emsc_min_mag)s+) for this window`;
        usgsIcon=`<span class="det-usgs-icon" style="color:#30363d" title="${escAttr(usgsTitle)}">&#10007;</span>`;
      } else {
        usgsIcon=`<span class="det-usgs-icon" style="color:#6e7681" title="Catalog lookup pending">&#8943;</span>`;
      }
      // full tooltip
      const place=det.usgs?(det.usgs.place||''):'';
      const magType=det.usgs?(det.usgs.magType||''):'';
      const mbNote=det.mb!=null?(det.mb_local?'local source (amp ratio > 5x)':det.mb_approx?`mb~${det.mb.toFixed(1)} IASPEI Δ≈45°`:`mb=${det.mb.toFixed(1)} IASPEI`):'mb pending';
      const detTitle=`${det.ts}\n${det.stations.join(', ')}\nconf: ${det.conf.toFixed(4)}  gap: ${(det.logit_gap||0).toFixed(1)}`
        +(det.epicenter?`\nepi: ${det.epicenter[0].toFixed(2)}N ${det.epicenter[1].toFixed(2)}E`:'')
        +`\n${mbNote}`
        +(det.usgs?(()=>{const src=(det.usgs.source||'usgs').toUpperCase();return `\n${src}: M${det.usgs.mag}${magType} — ${place}`;})():det.usgs_checked?`\nNo catalog match (USGS M%(usgs_min_mag)s+ / EMSC M%(emsc_min_mag)s+)`:`\nCatalog lookup pending`);
      const selCls=det.ts===selectedDetTs?' det-selected':'';
      const rowClick=(!det.teleseismic&&det.epicenter)
        ?`onclick="flyToEpi(${det.epicenter[0]},${det.epicenter[1]},'${det.ts}')" style="cursor:pointer"`:'';
      return sep+`<div class="det${selCls}" ${rowClick} title="${escAttr(detTitle)}">
        <span class="det-time">${tPart}</span>
        <span class="det-stas">${det.stations.join(' · ')}</span>
        <span class="det-chips-inline">${mbChip}${epiChip}</span>
        ${usgsIcon}
        <span class="det-age">${fmtAge(det.unix_ts)}</span>
      </div>`;
    }).join('');
    if(dDiv.innerHTML!==newHtml)dDiv.innerHTML=newHtml;
    // epicenter markers
    detMarkers.forEach(({m})=>map.removeLayer(m));
    detMarkers.length=0;
    d.detections.forEach(det=>{
      if(!det.epicenter||det.teleseismic)return;
      const [la,lo]=det.epicenter;
      const mb=det.mb||4;
      const r=Math.max(4,Math.min(14,(mb-2)*3+4));
      const mbLabel=det.mb?(det.mb_local?'local':det.mb_approx?'mb~'+det.mb.toFixed(1):'mb='+det.mb.toFixed(1)):'mb pending';
      const m=L.circleMarker([la,lo],{radius:r,color:'#f85149',fillColor:'#f85149',fillOpacity:.6})
        .bindPopup(`${fmtLocal(det.ts)}<br>${det.stations.join(', ')}<br>${mbLabel}`).addTo(map);
      detMarkers.push({m,ts:det.ts,r});
    });
    applyMarkerSelection();
    // flyTo newest non-teleseismic epicenter when it first appears
    const newestEpi=dets.find(det=>det.epicenter&&!det.teleseismic);
    if(newestEpi && newestEpi.ts!==lastFlyTs){
      lastFlyTs=newestEpi.ts;
      const [la,lo]=newestEpi.epicenter;
      map.getPane('overlayPane').style.visibility='hidden';
      map.flyTo([la,lo],5,{duration:2.5});
      map.once('moveend',()=>{map.getPane('overlayPane').style.visibility='';applyMarkerSelection();});
    }
    // fullscreen overlay: station list + latest detection
    const fsoSta=document.getElementById('fso-stations');
    const fsoDet=document.getElementById('fso-det');
    if(fsoSta){
      fsoSta.innerHTML=Object.entries(d.stations).sort((a,b)=>b[1].conf-a[1].conf).map(([k,s])=>{
        const col=confColor(s.conf);
        const pct=Math.round(s.conf*100);
        return `<div class="fso-sta"><span style="color:#58a6ff">${k}</span><span style="color:${col}">${s.conf.toFixed(3)}</span></div>
          <div class="fso-bar"><div class="fso-bar-fill" style="width:${pct}%;background:${col}"></div></div>`;
      }).join('');
    }
    if(fsoDet&&dets.length){
      const ld=dets[0];
      const mbStr=ld.mb!=null?(ld.mb_local?'local':ld.mb_approx?`mb~${ld.mb.toFixed(1)}`:`mb=${ld.mb.toFixed(1)}`):'mb…';
      const usgsStr=ld.usgs?(()=>{const src=(ld.usgs.source||'usgs').toUpperCase();return `${src}: M${ld.usgs.mag} ${(ld.usgs.place||'').split(',')[0]}`;})():ld.usgs_checked?'no catalog match':'catalog pending';
      fsoDet.innerHTML=`<div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Latest Detection</div>
        <div style="color:#e6edf3">${fmtLocal(ld.ts)}</div>
        <div style="color:#58a6ff;margin:2px 0">${ld.stations.join(' · ')}</div>
        <div style="color:#d29922">${mbStr}</div>
        ${ld.epicenter?`<div style="color:#d29922">${ld.epicenter[0].toFixed(2)}N ${ld.epicenter[1].toFixed(2)}E</div>`:''}
        <div style="color:#a371f7;margin-top:2px">${usgsStr}</div>`;
    }
  }).catch(()=>{document.getElementById('status-dot').style.background='#f85149'});
}
update();setInterval(update,3000);
</script>
</body>
</html>"""

def start_web_server():
    if WEB_PORT == 0:
        return
    try:
        from flask import Flask, jsonify
    except ImportError:
        print("flask not installed — web UI disabled (pip install flask)", flush=True)
        return

    coords_json = json.dumps({k: list(v) for k, v in station_coords.items()})
    sta_list    = ', '.join(f"{n}.{s}" for n, s in STATIONS)
    cfg_text    = f"threshold {THRESHOLD} | {N_CONSENSUS}/{len(STATIONS)} consensus | {CONSENSUS_WINDOW:.0f}s window"
    app_title   = f"{sta_list} | fra"
    html = (
        _WEB_HTML
        .replace('%(station_coords_json)s', coords_json)
        .replace('%(app_title)s',           app_title)
        .replace('%(cfg_text)s',            cfg_text)
        .replace('%(seedlink)s',            SEEDLINK_SERVER)
        .replace('%(threshold)s',           str(THRESHOLD))
        .replace('%(usgs_min_mag)s',        str(USGS_MIN_MAG))
        .replace('%(emsc_min_mag)s',        str(EMSC_MIN_MAG))
    )

    app = Flask(__name__)
    import logging
    from flask import request, Response
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    # Simple in-memory per-IP rate limiter: max 60 req/min on API endpoint
    _rate_buckets = {}
    _rate_lock    = threading.Lock()
    _RATE_LIMIT   = 60    # requests
    _RATE_WINDOW  = 60.0  # seconds

    def _check_rate(ip):
        now = time.time()
        with _rate_lock:
            if ip not in _rate_buckets:
                _rate_buckets[ip] = []
            bucket = _rate_buckets[ip]
            # prune old entries
            _rate_buckets[ip] = [t for t in bucket if now - t < _RATE_WINDOW]
            if len(_rate_buckets[ip]) >= _RATE_LIMIT:
                return False
            _rate_buckets[ip].append(now)
            # prune stale IPs periodically
            if len(_rate_buckets) > 500:
                cutoff = now - _RATE_WINDOW
                for k in list(_rate_buckets):
                    if all(t < cutoff for t in _rate_buckets[k]):
                        del _rate_buckets[k]
            return True

    @app.route('/')
    def index():
        return html

    @app.route('/health')
    def health():
        return Response('ok', status=200, mimetype='text/plain')

    @app.route('/api/state')
    def state():
        ip = request.headers.get('Fly-Client-IP') or request.remote_addr or 'unknown'
        if not _check_rate(ip):
            return Response('rate limited', status=429, mimetype='text/plain')
        return jsonify(sensor_state.to_dict())

    t = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=WEB_PORT, threaded=True),
        daemon=True,
        name='web-ui',
    )
    t.start()
    print(f"Web UI: http://0.0.0.0:{WEB_PORT}", flush=True)

# ── Rich TUI ───────────────────────────────────────────────────────────────────
def run_tui():
    try:
        from rich.live import Live
        from rich.table import Table
        from rich.panel import Panel
        from rich.layout import Layout
        from rich.columns import Columns
        from rich import box
    except ImportError:
        print("rich not installed — TUI disabled (pip install rich)", flush=True)
        return

    def build_display():
        snap = sensor_state.to_dict()
        now  = snap['now']

        sta_tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan",
                        title="[bold]Stations[/bold]", min_width=42)
        sta_tbl.add_column("Station", style="cyan")
        sta_tbl.add_column("Conf",    justify="right")
        sta_tbl.add_column("Mag",     justify="right")
        sta_tbl.add_column("Age",     justify="right", style="dim")
        for key, s in sorted(snap['stations'].items()):
            age = now - s['last_ts']
            c   = s['conf']
            color = "green" if c >= THRESHOLD else "yellow" if c > 0.5 else "dim"
            sta_tbl.add_row(key,
                            f"[{color}]{c:.3f}[/{color}]",
                            fmt_mag(s['mag_est']),
                            f"{age:.0f}s")

        det_lines = []
        for det in reversed(snap['detections'][-12:]):
            mb  = f"[green]mb={det['mb']:.1f}[/green]" if det['mb'] is not None else "[dim]mb…[/dim]"
            epi = ""
            if det['epicenter']:
                la, lo = det['epicenter']
                epi = f"  [yellow]{abs(la):.1f}°{'N' if la>=0 else 'S'} {abs(lo):.1f}°{'E' if lo>=0 else 'W'}[/yellow]"
            sta_str = ', '.join(det['stations'])
            det_lines.append(f"[dim]{det['ts']}[/dim]  [cyan]{sta_str}[/cyan]  {mb}{epi}")

        det_panel = Panel(
            '\n'.join(det_lines) if det_lines else "[dim]No detections yet[/dim]",
            title="[bold]Detections[/bold]",
        )
        layout = Layout()
        layout.split_column(
            Layout(Panel(sta_tbl), size=len(snap['stations']) + 6, name="stations"),
            Layout(det_panel, name="detections"),
        )
        return layout

    with Live(build_display(), refresh_per_second=1, screen=True) as live:
        while True:
            live.update(build_display())
            time.sleep(1)

# ── SeedLink client ────────────────────────────────────────────────────────────
def seedlink_loop(server, stations, models):
    """Connect to a SeedLink server, stream data for given stations, run inference. Retries forever."""
    from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

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

            conf, mag_est, logit_gap = ensemble_predict(models, normalize_window(window))
            on_inference(net, sta, conf, mag_est, logit_gap, time.time())

    print(f"\nConnecting to {server} ({len(stations)} station(s))...", flush=True)
    backoff = 5
    while True:
        try:
            client = Sensor(server)
            for net, sta in stations:
                for ch in CHANNELS:
                    client.select_stream(net, sta, ch)
            backoff = 5
            client.run()
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
            break
        except BaseException as e:
            print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] [{server}] "
                  f"Connection error ({type(e).__name__}): {e}", flush=True)
            print(f"  Retrying in {backoff}s...", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def run_sensor(models):
    init_station_state()
    sensor_state.detections = _load_detections()

    # Start Flask immediately so /health responds during rolling deploy health checks.
    # SeedLink connects later after the network-ready delay.
    start_web_server()

    startup_delay = int(os.environ.get('STARTUP_DELAY', '8'))
    if startup_delay > 0:
        print(f"Waiting {startup_delay}s for network...", flush=True)
        time.sleep(startup_delay)

    print(f"\nFetching station coordinates...", flush=True)
    try:
        fetch_station_coords()
    except Exception as e:
        print(f"  coords fetch failed ({e}) — using hardcoded fallback", flush=True)
        for net, sta in ALL_STATIONS:
            key = f"{net}.{sta}"
            if key in KNOWN_COORDS:
                station_coords[key] = KNOWN_COORDS[key]

    station_list = ', '.join(f"{n}.{s}" for n, s in ALL_STATIONS)
    print(f"  Stations:  {station_list}", flush=True)
    print(f"  Channels:  {CHANNELS}", flush=True)
    print(f"  Threshold: {THRESHOLD}  |  Consensus: {N_CONSENSUS}/{len(ALL_STATIONS)} in {CONSENSUS_WINDOW:.0f}s", flush=True)
    print(f"  Cooldown:  {ALERT_COOLDOWN}s  |  P-vel: {P_VEL_KM_S} km/s", flush=True)
    print(f"  Localize:  {LOC_MIN_STA}+ stations required", flush=True)

    # Build per-server station groups; always run GEOFON, optionally IRIS
    server_groups = [(SEEDLINK_SERVER, STATIONS)]
    if IRIS_STATIONS:
        server_groups.append((IRIS_SERVER, IRIS_STATIONS))

    if TUI_MODE:
        print("TUI mode — starting Rich display...", flush=True)
        for srv, stas in server_groups:
            t = threading.Thread(target=seedlink_loop, args=(srv, stas, models),
                                 daemon=True, name=f'seedlink-{srv.split(":")[0]}')
            t.start()
        run_tui()
    elif len(server_groups) == 1:
        print("Ready. Ctrl+C to stop.\n", flush=True)
        seedlink_loop(server_groups[0][0], server_groups[0][1], models)
    else:
        print("Ready. Ctrl+C to stop.\n", flush=True)
        for srv, stas in server_groups[1:]:
            t = threading.Thread(target=seedlink_loop, args=(srv, stas, models),
                                 daemon=True, name=f'seedlink-{srv.split(":")[0]}')
            t.start()
        seedlink_loop(server_groups[0][0], server_groups[0][1], models)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"Seismic Detection Sensor (multi-station consensus + TDOA localization)", flush=True)
    print(f"  model:   StreamingNet {N_SEEDS}-seed ensemble (H-{P_LEAD_S}s, mean-conf)", flush=True)
    print(f"  device:  {DEVICE}", flush=True)
    print(f"\nLoading checkpoints from {CHECKPOINT_DIR}...", flush=True)
    models = load_ensemble()
    print(f"  {N_SEEDS} models loaded.\n", flush=True)
    run_sensor(models)
