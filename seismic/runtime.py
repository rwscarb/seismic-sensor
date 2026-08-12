"""
runtime.py — mutable runtime overrides for the seismic sensor.

State here survives for the process lifetime only (not persisted across restarts).
Commands: /seismic mute [minutes], /seismic unmute, /seismic sensitivity <0-1>
"""
import threading
import time

from seismic.config import THRESHOLD

_lock = threading.Lock()

# Mute state
_muted_until: float = 0.0          # unix timestamp; 0 = not muted
_muted_by: str = ''                 # slack user who muted

# Threshold override
_threshold_override: float = None   # None = use config default


def mute(duration_minutes: float, by: str = ''):
    with _lock:
        global _muted_until, _muted_by
        _muted_until = time.time() + duration_minutes * 60
        _muted_by = by


def unmute(by: str = ''):
    with _lock:
        global _muted_until, _muted_by
        _muted_until = 0.0
        _muted_by = by


def set_threshold(value: float):
    with _lock:
        global _threshold_override
        _threshold_override = max(0.0, min(1.0, value))


def reset_threshold():
    with _lock:
        global _threshold_override
        _threshold_override = None


def is_muted() -> bool:
    with _lock:
        return time.time() < _muted_until


def get_threshold() -> float:
    with _lock:
        if _threshold_override is not None:
            return _threshold_override
    return THRESHOLD


def status_dict() -> dict:
    with _lock:
        now = time.time()
        muted = now < _muted_until
        return {
            'muted': muted,
            'muted_until': _muted_until if muted else None,
            'muted_remaining_s': max(0, int(_muted_until - now)) if muted else 0,
            'muted_by': _muted_by if muted else '',
            'threshold': _threshold_override if _threshold_override is not None else THRESHOLD,
            'threshold_override': _threshold_override is not None,
        }
