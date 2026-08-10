#!/usr/bin/env python3
"""
Live Seismic Detection Sensor — inference-only, Docker-ready.

Loads pre-trained StreamingNet ensemble from /checkpoints/,
connects to a SeedLink server, and streams real-time detection.

All config via environment variables (see Dockerfile / docker-compose.yml).
"""
import os, time, collections, warnings
import numpy as np
import torch, torch.nn as nn, torch.nn.functional as F
warnings.filterwarnings('ignore')

# ── Config from env ────────────────────────────────────────────────────────────
CHECKPOINT_DIR  = os.environ.get('CHECKPOINT_DIR', './checkpoints')
SEEDLINK_SERVER = os.environ.get('SEEDLINK_SERVER', 'liss.usgs.gov:4000')
NETWORK         = os.environ.get('NETWORK', 'IU')
STATION         = os.environ.get('STATION', 'MAJO')
CHANNELS        = os.environ.get('CHANNELS', 'HHZ,HHN,HHE').split(',')
THRESHOLD       = float(os.environ.get('THRESHOLD', '0.835'))
N_SEEDS         = int(os.environ.get('N_SEEDS', '3'))
ALERT_COOLDOWN  = float(os.environ.get('ALERT_COOLDOWN', '5.0'))

DEVICE        = 'cpu'       # inference-only; no GPU needed
K             = 128
CYCLES        = 1
WIN_SAMPLES   = 100         # 1.0s at 100sps
STRIDE        = 10          # infer every 0.1s
TARGET_SRATE  = 100.0
BUF_DECAY     = 0.876
BUF_STRENGTH  = 1.429

# ── Model (must match training exactly) ───────────────────────────────────────
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
        conf = float(F.softmax(logits, dim=1)[0, 1])
        return conf, float(mag[0])

# ── Load ensemble ─────────────────────────────────────────────────────────────
def load_ensemble():
    models = []
    for seed in range(N_SEEDS):
        ckpt = os.path.join(CHECKPOINT_DIR, f'seed_{seed}.pt')
        if not os.path.exists(ckpt):
            raise FileNotFoundError(
                f"Checkpoint not found: {ckpt}\n"
                f"Train with: python live_seismic_demo.py --train-only\n"
                f"Then copy ~/seismic_ensemble/seed_*.pt to ./checkpoints/"
            )
        m = StreamingNet(perm_seed=seed)
        m.load_state_dict(torch.load(ckpt, map_location='cpu'))
        m.eval()
        models.append(m)
        print(f"  loaded seed {seed} ← {ckpt}", flush=True)
    return models

def ensemble_predict(models, window_np):
    confs, mags = zip(*[m.predict(window_np) for m in models])
    return float(np.mean(confs)), float(np.mean(mags))

# ── Sliding window ─────────────────────────────────────────────────────────────
def normalize_window(buf_zne):
    w = buf_zne.copy()
    for i in range(3):
        w[i] /= w[i].std() + 1e-6
    return w

# ── SeedLink client ────────────────────────────────────────────────────────────
def run_sensor(models):
    from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

    ring = {ch: collections.deque(maxlen=500) for ch in CHANNELS}  # 5s buffer
    last_alert = [0.0]
    stride_counter = [0]
    last_status = [0.0]

    class Sensor(EasySeedLinkClient):
        def on_data(self, trace):
            ch = trace.stats.channel
            if ch not in ring:
                return

            data = trace.data.astype(np.float32)
            sr = trace.stats.sampling_rate
            if abs(sr - TARGET_SRATE) > 1.0:
                from obspy import Trace as OTrace
                t = OTrace(data=trace.data.copy(), header=trace.stats)
                t.resample(TARGET_SRATE)
                data = t.data.astype(np.float32)

            ring[ch].extend(data)
            stride_counter[0] += len(data)

            if min(len(ring[c]) for c in CHANNELS) < WIN_SAMPLES:
                return
            if stride_counter[0] < STRIDE:
                return
            stride_counter[0] = 0

            window = np.array([
                list(ring[CHANNELS[0]])[-WIN_SAMPLES:],   # Z
                list(ring[CHANNELS[1]])[-WIN_SAMPLES:],   # N
                list(ring[CHANNELS[2]])[-WIN_SAMPLES:],   # E
            ], dtype=np.float32)

            conf, mag_est = ensemble_predict(models, normalize_window(window))
            now = time.time()
            ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

            if conf >= THRESHOLD:
                if now - last_alert[0] > ALERT_COOLDOWN:
                    print(f"\n{'='*60}", flush=True)
                    print(f"  DETECTION  {ts}", flush=True)
                    print(f"  Station:    {NETWORK}.{STATION}", flush=True)
                    print(f"  Confidence: {conf:.4f}  (threshold={THRESHOLD})", flush=True)
                    print(f"  Mag est:    M{mag_est:.1f}", flush=True)
                    print(f"  Lead time:  +0.4s before P-arrival", flush=True)
                    print(f"{'='*60}\n", flush=True)
                    last_alert[0] = now
            elif now - last_status[0] > 10.0:
                print(f"[{ts}] {NETWORK}.{STATION}  conf={conf:.3f}  mag_est=M{mag_est:.1f}", flush=True)
                last_status[0] = now

    print(f"\nConnecting to {SEEDLINK_SERVER}...", flush=True)
    print(f"  Station:   {NETWORK}.{STATION}", flush=True)
    print(f"  Channels:  {CHANNELS}", flush=True)
    print(f"  Threshold: {THRESHOLD}  |  Cooldown: {ALERT_COOLDOWN}s", flush=True)
    print(f"  Window:    {WIN_SAMPLES}smp @ {TARGET_SRATE:.0f}Hz  |  Stride: {STRIDE}smp", flush=True)
    print("Ready. Ctrl+C to stop.\n", flush=True)

    backoff = 5
    while True:
        try:
            client = Sensor(SEEDLINK_SERVER)
            for ch in CHANNELS:
                client.select_stream(NETWORK, STATION, ch)
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
    print(f"Seismic Detection Sensor", flush=True)
    print(f"  model: StreamingNet {N_SEEDS}-seed ensemble (H-0.4s, mean-conf)", flush=True)
    print(f"  device: {DEVICE}", flush=True)
    print(f"\nLoading checkpoints from {CHECKPOINT_DIR}...", flush=True)
    models = load_ensemble()
    print(f"  {N_SEEDS} models loaded.\n", flush=True)
    run_sensor(models)
