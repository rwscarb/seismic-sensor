"""Persisted per-station confidence log — survives process restarts and
outlives Fly's log buffer (which only retains a couple minutes), so a
missed detection can actually be root-caused after the fact instead of
just "the log's already gone" (incident 2026-08-19: a missed M5.5 near
Ruteng, Indonesia was already unreachable 23 minutes later).

Compact TSV, one file per UTC day, old files pruned on write — this is a
high-volume log (every station, every inference callback) against a small
1GB volume, so format and retention both matter.
"""

import os
import time

from seismic.config import STATION_LOG_DIR, STATION_LOG_RETENTION_DAYS

_last_prune_day = None


def _day_str(now: float) -> str:
    return time.strftime('%Y-%m-%d', time.gmtime(now))


def _prune_old(now: float):
    global _last_prune_day
    today = _day_str(now)
    if today == _last_prune_day:
        return
    _last_prune_day = today
    try:
        cutoff = now - STATION_LOG_RETENTION_DAYS * 86400
        for name in os.listdir(STATION_LOG_DIR):
            if not name.endswith('.tsv'):
                continue
            try:
                file_day = time.strptime(name[:-4], '%Y-%m-%d')
            except ValueError:
                continue
            if time.mktime(file_day) < cutoff:
                os.remove(os.path.join(STATION_LOG_DIR, name))
    except OSError:
        pass  # dir may not exist yet on first run — created below on write


def log_station_reading(now: float, key: str, conf: float, mag_est: float,
                         status: str = '', logit_gap: float | None = None,
                         stalta_ratio: float | None = None):
    """Append one compact row: unix_ts, station, conf, mag_est, status,
    logit_gap, stalta_ratio. status is one of '' (routine), 'candidate',
    'gated', 'fired', 'rescue', 'noisy', 'flatline' — matches the
    print()-based classification already in consensus.on_inference."""
    try:
        os.makedirs(STATION_LOG_DIR, exist_ok=True)
        _prune_old(now)
        path = os.path.join(STATION_LOG_DIR, f'{_day_str(now)}.tsv')
        gap_s = f'{logit_gap:.3f}' if logit_gap is not None else ''
        stalta_s = f'{stalta_ratio:.2f}' if stalta_ratio is not None else ''
        with open(path, 'a') as f:
            f.write(f'{now:.3f}\t{key}\t{conf:.4f}\t{mag_est:.2f}\t{status}\t{gap_s}\t{stalta_s}\n')
    except OSError:
        pass  # diagnostic logging must never take down detection itself


def read_range(start: float, end: float) -> list[dict]:
    """Read all rows across whatever daily files overlap [start, end] —
    the read side of the diagnostic tool, e.g. for a future `ott`-style
    'what did every station see around time X' command."""
    out = []
    try:
        days = set()
        t = start - 86400  # cover UTC day boundaries generously
        while t <= end + 86400:
            days.add(_day_str(t))
            t += 86400
        for day in sorted(days):
            path = os.path.join(STATION_LOG_DIR, f'{day}.tsv')
            if not os.path.isfile(path):
                continue
            with open(path) as f:
                for line in f:
                    parts = line.rstrip('\n').split('\t')
                    if len(parts) != 7:
                        continue
                    ts = float(parts[0])
                    if start <= ts <= end:
                        out.append({
                            'ts': ts, 'station': parts[1], 'conf': float(parts[2]),
                            'mag_est': float(parts[3]), 'status': parts[4],
                            'logit_gap': float(parts[5]) if parts[5] else None,
                            'stalta_ratio': float(parts[6]) if parts[6] else None,
                        })
    except OSError:
        pass
    return sorted(out, key=lambda r: r['ts'])
