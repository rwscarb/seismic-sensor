#!/usr/bin/env python3
"""
Live Seismic Detection Sensor — multi-station consensus, inference-only, Docker-ready.

Loads pre-trained StreamingNet ensemble from /checkpoints/,
connects to a SeedLink server, runs inference on each station independently,
and alerts only when N_CONSENSUS stations agree within CONSENSUS_WINDOW seconds.

All config via environment variables (see .env / fly.toml).

STATIONS format: "GE.APE,GE.MORC"  (NET.STA pairs, comma-separated)
"""
import os, time, collections, warnings
import numpy as np
import torch, torch.nn as nn, torch.nn.functional as F
warnings.filterwarnings('ignore')

# ── Config from env ────────────────────────────────────────────────────────────
CHECKPOINT_DIR   = os.environ.get('CHECKPOINT_DIR', './checkpoints')
SEEDLINK_SERVER  = os.environ.get('SEEDLINK_SERVER', 'geofon.gfz-potsdam.de:18000')
STATIONS_RAW     = os.environ.get('STATIONS', 'GE.APE')   # NET.STA,NET.STA,...
CHANNELS         = os.environ.get('CHANNELS', 'HHZ,HHN,HHE').split(',')
THRESHOLD        = float(os.environ.get('THRESHOLD', '0.835'))
N_SEEDS          = int(os.environ.get('N_SEEDS', '3'))
ALERT_COOLDOWN   = float(os.environ.get('ALERT_COOLDOWN', '60.0'))
N_CONSENSUS      = int(os.environ.get('N_CONSENSUS', '1'))   # stations required
CONSENSUS_WINDOW = float(os.environ.get('CONSENSUS_WINDOW', '120.0'))  # seconds

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

# ── Multi-station consensus state ─────────────────────────────────────────────
# station_key → deque of ring buffers per channel
station_rings   = {}   # key → {ch: deque}
station_strides = {}   # key → int (stride counter)
station_status  = {}   # key → float (last status print time)

# Recent detections for consensus: deque of (timestamp, station_key, conf, mag)
recent_detections = collections.deque()

last_alert = [0.0]

def station_key(net, sta):
    return f"{net}.{sta}"

def init_station_state():
    for net, sta in STATIONS:
        k = station_key(net, sta)
        station_rings[k]   = {ch: collections.deque(maxlen=500) for ch in CHANNELS}
        station_strides[k] = 0
        station_status[k]  = 0.0

def check_consensus(now):
    """Return True if N_CONSENSUS distinct stations fired within CONSENSUS_WINDOW."""
    cutoff = now - CONSENSUS_WINDOW
    recent_detections_pruned = [d for d in recent_detections if d[0] >= cutoff]
    recent_detections.clear()
    recent_detections.extend(recent_detections_pruned)
    stations_fired = set(d[1] for d in recent_detections)
    return len(stations_fired) >= N_CONSENSUS, stations_fired

def on_inference(net, sta, conf, mag_est, now):
    """Called after each inference step for a station."""
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    key = station_key(net, sta)

    if conf >= THRESHOLD:
        recent_detections.append((now, key, conf, mag_est))
        consensus_met, stations_fired = check_consensus(now)

        if consensus_met and now - last_alert[0] > ALERT_COOLDOWN:
            last_alert[0] = now
            recent_detections.clear()  # reset so coda windows don't re-trigger
            mag_display = max(-2.0, min(9.9, mag_est))
            station_list = ', '.join(sorted(stations_fired))
            print(f"\n{'='*60}", flush=True)
            print(f"  DETECTION  {ts}", flush=True)
            print(f"  Stations:   {station_list}  ({len(stations_fired)}/{N_CONSENSUS} consensus)", flush=True)
            print(f"  Confidence: {conf:.4f}  (threshold={THRESHOLD})", flush=True)
            print(f"  Mag est:    M{mag_display:.1f}  (uncalibrated)", flush=True)
            print(f"  Lead time:  +0.4s before P-arrival", flush=True)
            print(f"{'='*60}\n", flush=True)
        elif not consensus_met:
            mag_display = max(-2.0, min(9.9, mag_est))
            print(f"  [{ts}] {key} CANDIDATE conf={conf:.3f} mag=M{mag_display:.1f} "
                  f"(waiting for {N_CONSENSUS - len(stations_fired)} more station(s))", flush=True)
    else:
        if now - station_status[key] > 10.0:
            mag_display = max(-2.0, min(9.9, mag_est))
            print(f"[{ts}] {key}  conf={conf:.3f}  mag=M{mag_display:.1f}", flush=True)
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

    print(f"\nConnecting to {SEEDLINK_SERVER}...", flush=True)
    station_list = ', '.join(f"{n}.{s}" for n, s in STATIONS)
    print(f"  Stations:  {station_list}", flush=True)
    print(f"  Channels:  {CHANNELS}", flush=True)
    print(f"  Threshold: {THRESHOLD}  |  Consensus: {N_CONSENSUS}/{len(STATIONS)} in {CONSENSUS_WINDOW:.0f}s", flush=True)
    print(f"  Cooldown:  {ALERT_COOLDOWN}s", flush=True)
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
    print(f"Seismic Detection Sensor (multi-station consensus)", flush=True)
    print(f"  model:   StreamingNet {N_SEEDS}-seed ensemble (H-0.4s, mean-conf)", flush=True)
    print(f"  device:  {DEVICE}", flush=True)
    print(f"\nLoading checkpoints from {CHECKPOINT_DIR}...", flush=True)
    models = load_ensemble()
    print(f"  {N_SEEDS} models loaded.\n", flush=True)
    run_sensor(models)
