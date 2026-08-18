"""
Training data collector.

Hooks into the detection + USGS pipeline to save labeled waveform windows.
Windows are saved as .npz files in TRAINING_DIR:
  {unix_ts:.3f}_{station}_pending.npz   — awaiting USGS verdict
  {unix_ts:.3f}_{station}_positive.npz  — USGS confirmed real event
  {unix_ts:.3f}_{station}_negative.npz  — no USGS match (false positive / noise)

Each .npz contains:
  window   (3, WIN_SAMPLES) float32  — normalized 3-channel waveform
  raw      (3, WIN_SAMPLES) float32  — unnormalized (for later feature analysis)
  unix_ts  scalar                    — detection unix timestamp
  station  str                       — station key e.g. "GE.MORC"
  conf     scalar                    — model conf at detection
  logit_gap scalar                   — model logit gap
  stalta   scalar                    — STA/LTA ratio at detection
  label    str                       — 'pending' | 'positive' | 'negative'

Noise windows are saved periodically for extra negative examples:
  {unix_ts:.3f}_{station}_noise.npz   — quiet period, not near a detection
"""
import os
import time
import threading
import numpy as np

from seismic.config import (
    CHANNELS, TARGET_SRATE, WIN_SAMPLES,
    STALTA_SHORT_S, STALTA_LONG_S,
)

TRAINING_DIR = os.environ.get('TRAINING_DIR', '/data/training')
COLLECT_ENABLED = os.environ.get('COLLECT', '1').lower() not in ('0', 'false', 'no')
NOISE_SAMPLE_INTERVAL = float(os.environ.get('NOISE_SAMPLE_INTERVAL', '300.0'))  # seconds between noise samples

_lock = threading.Lock()
_last_noise_sample: dict = {}  # key → float


def _ensure_dir():
    os.makedirs(TRAINING_DIR, exist_ok=True)


def _stalta(data: np.ndarray) -> float:
    short_n = max(1, int(STALTA_SHORT_S * TARGET_SRATE))
    long_n = max(short_n + 1, int(STALTA_LONG_S * TARGET_SRATE))
    if len(data) < long_n:
        return 0.0
    buf = data[-long_n:].astype(np.float64)
    sq = buf ** 2
    lta = float(np.mean(sq))
    if lta < 1e-30:
        return 0.0
    sta = float(np.mean(sq[-short_n:]))
    return sta / lta


def _window_from_ring(ring: dict, channels: list) -> tuple:
    """Extract current WIN_SAMPLES from ring buffers.
    Returns (normalized, raw) both shape (3, WIN_SAMPLES), or (None, None) if insufficient data."""
    if any(len(ring.get(ch, [])) < WIN_SAMPLES for ch in channels):
        return None, None
    raw = np.array([
        list(ring[ch])[-WIN_SAMPLES:]
        for ch in channels
    ], dtype=np.float32)
    from seismic.model import normalize_window  # noqa: PLC0415
    normed = normalize_window(raw)
    return normed, raw


def save_detection_window(station_key: str, unix_ts: float, conf: float,
                          logit_gap: float, stalta_ratio: float):
    """Called at detection time — saves window as 'pending', to be labeled later."""
    if not COLLECT_ENABLED:
        return
    try:
        from seismic.consensus import station_rings  # late import
        if station_key not in station_rings:
            return
        normed, raw = _window_from_ring(station_rings[station_key], CHANNELS)
        if normed is None:
            return
        _ensure_dir()
        fname = f'{unix_ts:.3f}_{station_key}_pending.npz'
        path = os.path.join(TRAINING_DIR, fname)
        np.savez_compressed(
            path,
            window=normed, raw=raw,
            unix_ts=np.float64(unix_ts),
            station=np.bytes_(station_key),
            conf=np.float32(conf),
            logit_gap=np.float32(logit_gap),
            stalta=np.float32(stalta_ratio),
            label=np.bytes_('pending'),
        )
    except Exception as e:
        print(f'[collector] save_detection_window failed: {e}', flush=True)


def label_detection(unix_ts: float, station_key: str, label: str):
    """Rename pending → positive/negative once USGS verdict is known.
    label must be 'positive' or 'negative'.
    Matches by unix_ts within ±1s across all stations if station_key is None.
    """
    if not COLLECT_ENABLED:
        return
    assert label in ('positive', 'negative')
    try:
        _ensure_dir()
        for fname in os.listdir(TRAINING_DIR):
            if '_pending.npz' not in fname:
                continue
            parts = fname.split('_')
            if len(parts) < 3:
                continue
            try:
                fts = float(parts[0])
            except ValueError:
                continue
            fsta = '_'.join(parts[1:-1])  # e.g. "GE.MORC"
            if abs(fts - unix_ts) > 1.0:
                continue
            if station_key and fsta != station_key:
                continue
            new_fname = fname.replace('_pending.npz', f'_{label}.npz')
            old_path = os.path.join(TRAINING_DIR, fname)
            new_path = os.path.join(TRAINING_DIR, new_fname)
            # Update the label field inside the file
            data = dict(np.load(old_path, allow_pickle=True))
            data['label'] = np.bytes_(label)
            np.savez_compressed(new_path, **data)
            os.remove(old_path)
    except Exception as e:
        print(f'[collector] label_detection failed: {e}', flush=True)


def maybe_save_noise_window(station_key: str, unix_ts: float, conf: float):
    """Called on every inference; saves a noise window if station has been quiet
    for NOISE_SAMPLE_INTERVAL seconds and conf is low (genuine quiet period)."""
    if not COLLECT_ENABLED or conf > 0.3:
        return
    with _lock:
        last = _last_noise_sample.get(station_key, 0.0)
        if unix_ts - last < NOISE_SAMPLE_INTERVAL:
            return
        _last_noise_sample[station_key] = unix_ts
    try:
        from seismic.consensus import station_rings  # late import
        if station_key not in station_rings:
            return
        normed, raw = _window_from_ring(station_rings[station_key], CHANNELS)
        if normed is None:
            return
        _ensure_dir()
        fname = f'{unix_ts:.3f}_{station_key}_noise.npz'
        path = os.path.join(TRAINING_DIR, fname)
        np.savez_compressed(
            path,
            window=normed, raw=raw,
            unix_ts=np.float64(unix_ts),
            station=np.bytes_(station_key),
            conf=np.float32(conf),
            logit_gap=np.float32(0.0),
            stalta=np.float32(0.0),
            label=np.bytes_('noise'),
        )
    except Exception as e:
        print(f'[collector] maybe_save_noise_window failed: {e}', flush=True)


def collection_stats() -> dict:
    """Return counts by label for /api/state or logging."""
    counts = {'positive': 0, 'negative': 0, 'noise': 0, 'pending': 0}
    try:
        if not os.path.isdir(TRAINING_DIR):
            return counts
        for fname in os.listdir(TRAINING_DIR):
            for label in counts:
                if f'_{label}.npz' in fname:
                    counts[label] += 1
    except Exception:
        pass
    return counts
