"""
Second-stage seismic event classifier.

Loaded lazily on first call. Runs on CPU (tiny model, fast enough).
Returns P(event) averaged across firing stations; caller decides threshold.
"""

import os
import logging

import numpy as np

log = logging.getLogger(__name__)

_MODEL_PATH = os.environ.get('CLASSIFIER_PATH', '/data/seismic_finetuned.pt')
_THRESHOLD  = float(os.environ.get('CLASSIFIER_THRESHOLD', '0.25'))

_model = None
_torch = None


# ── Architecture (must match training) ────────────────────────────────────────

def _build_model():
    import torch.nn as nn

    class ConvBlock(nn.Module):
        def __init__(self, in_ch, out_ch, k=5):
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv1d(in_ch, out_ch, k, padding=k // 2),
                nn.BatchNorm1d(out_ch),
                nn.GELU(),
            )
        def forward(self, x):
            return self.net(x)

    class SeismicClassifier(nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = nn.Sequential(
                ConvBlock(3, 32),
                nn.MaxPool1d(2),
                ConvBlock(32, 64),
                nn.MaxPool1d(2),
                ConvBlock(64, 128),
                nn.AdaptiveAvgPool1d(8),
            )
            self.head = nn.Sequential(
                nn.Flatten(),
                nn.Linear(128 * 8, 128),
                nn.GELU(),
                nn.Dropout(0.3),
                nn.Linear(128, 1),
            )
        def forward(self, x):
            return self.head(self.encoder(x)).squeeze(-1)

    return SeismicClassifier()


def _load():
    global _model, _torch
    if _model is not None:
        return True
    if not os.path.exists(_MODEL_PATH):
        log.warning('[classifier] model not found at %s — second-stage disabled', _MODEL_PATH)
        return False
    try:
        import torch
        _torch = torch
        m = _build_model()
        m.load_state_dict(torch.load(_MODEL_PATH, map_location='cpu', weights_only=True))
        m.eval()
        _model = m
        log.info('[classifier] loaded from %s (threshold=%.2f)', _MODEL_PATH, _THRESHOLD)
        return True
    except Exception as e:
        log.error('[classifier] load failed: %s', e)
        return False


def _window_from_ring(ring, channels, win_samples):
    """Replicate collector._window_from_ring: last win_samples, per-channel std-norm."""
    if any(len(ring.get(ch, [])) < win_samples for ch in channels):
        return None
    raw = np.array([
        list(ring[ch])[-win_samples:]
        for ch in channels
    ], dtype=np.float32)
    normed = raw.copy()
    for i in range(len(channels)):
        normed[i] /= normed[i].std() + 1e-6
    return normed


def score(stations_fired, station_rings, channels, win_samples):
    """
    Return (mean_prob, should_suppress).

    mean_prob       — average P(event) across firing stations (0–1)
    should_suppress — True if mean_prob < _THRESHOLD (veto the detection)
    """
    if not _load():
        return None, False

    probs = []
    for k in stations_fired:
        ring = station_rings.get(k)
        if not ring:
            continue
        window = _window_from_ring(ring, channels, win_samples)
        if window is None:
            continue
        x = _torch.tensor(window, dtype=_torch.float32).unsqueeze(0)  # (1, 3, W)
        with _torch.no_grad():
            prob = _torch.sigmoid(_model(x)).item()
        probs.append(prob)

    if not probs:
        return None, False

    mean_prob = float(np.mean(probs))
    return mean_prob, mean_prob < _THRESHOLD
