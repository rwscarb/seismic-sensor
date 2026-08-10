import os
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from seismic.config import (
    CHECKPOINT_DIR, N_SEEDS, K, CYCLES, BUF_DECAY, BUF_STRENGTH,
    TEMP_SCALE, CHANNELS, TARGET_SRATE,
)

# S-P time → distance (km). Wadati / IASP91 average for teleseismic P+S.
# For Δ 10–100°: dist ≈ Δt_sp × 9.5  (V_P≈8.5, V_S≈4.8 km/s harmonic mean)
_SP_KM_PER_S = 9.5


class ConvBlock(nn.Module):
    def __init__(self, ci, co, k=7):
        super().__init__()
        self.net = nn.Sequential(nn.Conv1d(ci, co, k, padding=k//2), nn.BatchNorm1d(co), nn.ReLU())

    def forward(self, x):
        return self.net(x)


class StreamingNet(nn.Module):
    def __init__(self, perm_seed=0):
        super().__init__()
        self.enc = nn.Sequential(ConvBlock(3, 32), ConvBlock(32, 64), ConvBlock(64, K),
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


# ── PhaseNet pick refinement (SeisBench) ──────────────────────────────────────
_phasenet = None


def load_phasenet():
    global _phasenet
    try:
        import seisbench.models as sbm
        _phasenet = sbm.PhaseNet.from_pretrained("original")
        _phasenet.eval()
        print("  PhaseNet loaded (SeisBench pretrained)", flush=True)
    except Exception as e:
        print(f"  PhaseNet unavailable ({e}) — TDOA will use StreamingNet picks only", flush=True)


def refine_picks_phasenet(p_arr_snapshot):
    """
    For each station that fired, extract a 30s window around its estimated P arrival
    from the ring buffers, run PhaseNet, and return refined P picks and S-P distances.

    Returns:
      refined_p   : {key: unix_time}  — replaces raw StreamingNet picks where PhaseNet
                                        has higher-confidence P (within ±3s of original)
      sp_distances: {key: km}         — distance from S-P time, where S is detected
    """
    if _phasenet is None:
        return dict(p_arr_snapshot), {}

    from obspy import Trace as OTrace, Stream as OStream, UTCDateTime
    from seismic.consensus import station_rings  # late import to avoid circular dependency

    refined_p    = dict(p_arr_snapshot)
    sp_distances = {}

    for key, p_est in p_arr_snapshot.items():
        if key not in station_rings:
            continue
        ring = station_rings[key]
        if any(len(ring.get(ch, [])) < int(30 * TARGET_SRATE) for ch in CHANNELS):
            continue

        buf_end   = time.time()
        buf_start = buf_end - len(ring[CHANNELS[0]]) / TARGET_SRATE

        # Extract [P - 5s, P + 25s] window (30s total = 3000 samples at 100 sps)
        win_start  = p_est - 5.0
        win_end    = p_est + 25.0
        idx_start  = max(0, int((win_start - buf_start) * TARGET_SRATE))
        idx_end    = min(len(ring[CHANNELS[0]]), int((win_end - buf_start) * TARGET_SRATE))
        actual_start = buf_start + idx_start / TARGET_SRATE

        try:
            st = OStream()
            for ch in CHANNELS:
                data = np.array(list(ring[ch]), dtype=np.float32)[idx_start:idx_end]
                if len(data) < 100:
                    break
                tr = OTrace(data=data)
                tr.stats.network       = key.split('.')[0]
                tr.stats.station       = key.split('.')[1]
                tr.stats.channel       = ch
                tr.stats.sampling_rate = TARGET_SRATE
                tr.stats.starttime     = UTCDateTime(actual_start)
                st.append(tr)
            else:
                picks = _phasenet.classify(st, batch_size=1).picks
                p_picks = [pk for pk in picks if pk.phase == 'P' and pk.peak_value >= 0.4]
                s_picks = [pk for pk in picks if pk.phase == 'S' and pk.peak_value >= 0.3]

                if p_picks:
                    best_p = max(p_picks, key=lambda pk: pk.peak_value)
                    p_unix = best_p.peak_time.timestamp
                    if abs(p_unix - p_est) <= 3.0:   # sanity check: within 3s of StreamingNet
                        refined_p[key] = p_unix
                        print(f"  [phasenet] {key} P refined by "
                              f"{p_unix - p_est:+.2f}s (conf={best_p.peak_value:.2f})", flush=True)

                if s_picks:
                    best_s = max(s_picks, key=lambda pk: pk.peak_value)
                    s_unix = best_s.peak_time.timestamp
                    p_used = refined_p.get(key, p_est)
                    sp_dt  = s_unix - p_used
                    if 5.0 <= sp_dt <= 1200.0:    # sanity: 5s–20min S-P
                        dist_km = sp_dt * _SP_KM_PER_S
                        sp_distances[key] = dist_km
                        print(f"  [phasenet] {key} S-P={sp_dt:.1f}s → "
                              f"dist≈{dist_km:.0f}km (conf={best_s.peak_value:.2f})", flush=True)
        except Exception as e:
            print(f"  [phasenet] {key}: {e}", flush=True)

    return refined_p, sp_distances


def normalize_window(w):
    w = w.copy()
    for i in range(3):
        w[i] /= w[i].std() + 1e-6
    return w
