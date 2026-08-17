"""
seismic.backfill — offline waveform fetch + inference for past events.

Given a past earthquake (lat, lon, origin_unix), fetches historical waveforms
from IRIS FDSN, runs them through the loaded ensemble with a sliding window,
and reports whether consensus would have fired.

Used by /api/backfill — models must already be loaded (runs inside the sensor process).
"""

import io
import time
import urllib.request

import numpy as np

from seismic.config import (
    CHANNELS, TARGET_SRATE, WIN_SAMPLES, STRIDE,
    N_CONSENSUS, CONSENSUS_WINDOW, THRESHOLD,
    STALTA_ON, STALTA_SHORT_S, STALTA_LONG_S, STALTA_THRESH,
    P_VEL_KM_S,
)
from seismic.localize import station_coords, haversine_km, p_travel_time_s
from seismic.model import ensemble_predict, normalize_window

_FDSN_BASE = 'https://service.iris.edu/fdsnws/dataselect/1/query'
_FETCH_PAD_S = 30.0     # seconds before expected P to start fetch
_FETCH_WIN_S = 120.0    # seconds of waveform to fetch per station


# ── IRIS FDSN waveform fetch ──────────────────────────────────────────────────

def _fetch_waveform(net: str, sta: str, t_start: float, t_end: float) -> dict | None:
    """
    Fetch miniSEED from IRIS FDSN and return {channel: np.ndarray} at TARGET_SRATE.
    Returns None on failure.
    """
    try:
        from obspy import read as obs_read, UTCDateTime
    except ImportError:
        return None

    start = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t_start))
    end   = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t_end))
    chan  = ','.join(CHANNELS)
    url   = (
        f'{_FDSN_BASE}?network={net}&station={sta}'
        f'&location=*&channel={chan}'
        f'&starttime={start}&endtime={end}&format=miniseed'
    )
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/octet-stream'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except Exception as e:
        return {'error': str(e)}

    try:
        st = obs_read(io.BytesIO(raw))
        st.detrend('demean')
        st.resample(TARGET_SRATE)
        result = {}
        for ch in CHANNELS:
            trs = [tr for tr in st if tr.stats.channel == ch]
            if trs:
                result[ch] = trs[0].data.astype(np.float32)
        return result if result else None
    except Exception as e:
        return {'error': str(e)}


# ── STA/LTA ───────────────────────────────────────────────────────────────────

def _stalta(data: np.ndarray) -> float:
    short_n = max(1, int(STALTA_SHORT_S * TARGET_SRATE))
    long_n  = max(short_n + 1, int(STALTA_LONG_S * TARGET_SRATE))
    if len(data) < long_n:
        return 0.0
    buf = data[-long_n:].astype(np.float64)
    sq  = buf ** 2
    lta = float(np.mean(sq))
    if lta < 1e-30:
        return 0.0
    return float(np.mean(sq[-short_n:])) / lta


# ── Sliding-window inference on a fetched waveform ────────────────────────────

def _run_inference(channels_data: dict, models) -> dict:
    """
    Slide WIN_SAMPLES window over the fetched traces, run ensemble.
    Returns {peak_conf, peak_mag, peak_stalta, peak_sample, fired}.
    """
    ch_arrays = [channels_data.get(ch) for ch in CHANNELS]
    if any(a is None for a in ch_arrays):
        return {'error': 'missing channels'}

    min_len = min(len(a) for a in ch_arrays)
    if min_len < WIN_SAMPLES:
        return {'error': f'too short ({min_len} samples, need {WIN_SAMPLES})'}

    peak_conf = 0.0
    peak_mag  = -9.9
    peak_stalta = 0.0
    peak_sample = 0

    for i in range(0, min_len - WIN_SAMPLES, STRIDE):
        window = np.array([a[i:i + WIN_SAMPLES] for a in ch_arrays], dtype=np.float32)
        stalta = _stalta(ch_arrays[0][max(0, i - int(STALTA_LONG_S * TARGET_SRATE)):i + WIN_SAMPLES])
        if STALTA_ON and stalta < STALTA_THRESH:
            continue
        conf, mag, _ = ensemble_predict(models, normalize_window(window))
        if conf > peak_conf:
            peak_conf   = conf
            peak_mag    = mag
            peak_stalta = stalta
            peak_sample = i

    return {
        'peak_conf':   round(peak_conf, 4),
        'peak_mag':    round(peak_mag, 2),
        'peak_stalta': round(peak_stalta, 2),
        'peak_offset_s': round(peak_sample / TARGET_SRATE, 1),
        'fired': peak_conf >= THRESHOLD,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def evaluate_event(event_lat: float, event_lon: float, origin_unix: float,
                   models, event_label: str = '') -> dict:
    """
    Fetch waveforms and run inference for all stations with known coordinates.
    Simulate consensus and return a full per-station report.
    """
    results = {}
    fired_stations = []
    fired_times   = []

    for key, (sta_lat, sta_lon) in station_coords.items():
        net, sta = key.split('.', 1)
        dist_km  = haversine_km(event_lat, event_lon, sta_lat, sta_lon)
        p_travel = p_travel_time_s(dist_km)
        p_arrival = origin_unix + p_travel

        t_start = p_arrival - _FETCH_PAD_S
        t_end   = p_arrival + _FETCH_WIN_S

        channels_data = _fetch_waveform(net, sta, t_start, t_end)
        if channels_data is None:
            results[key] = {'status': 'no_data', 'dist_km': round(dist_km, 0)}
            continue
        if 'error' in channels_data:
            results[key] = {'status': 'fetch_error', 'error': channels_data['error'],
                            'dist_km': round(dist_km, 0)}
            continue

        inf = _run_inference(channels_data, models)
        inf['dist_km']    = round(dist_km, 0)
        inf['p_travel_s'] = round(p_travel, 0)
        inf['p_arrival']  = time.strftime('%H:%M:%SZ', time.gmtime(p_arrival))
        inf['status']     = 'ok'

        results[key] = inf
        if inf.get('fired'):
            fired_stations.append(key)
            fired_times.append(p_arrival + inf.get('peak_offset_s', 0))

    # Simulate consensus: do we have N_CONSENSUS stations firing within CONSENSUS_WINDOW?
    consensus_fired = False
    consensus_group = []
    fired_times_sorted = sorted(zip(fired_times, fired_stations))
    for i, (t0, s0) in enumerate(fired_times_sorted):
        group = [(t0, s0)]
        for t1, s1 in fired_times_sorted[i+1:]:
            if t1 - t0 <= CONSENSUS_WINDOW:
                group.append((t1, s1))
        if len(group) >= N_CONSENSUS:
            consensus_fired = True
            consensus_group = [s for _, s in group[:N_CONSENSUS]]
            break

    return {
        'event': {
            'label':      event_label,
            'lat':        event_lat,
            'lon':        event_lon,
            'origin_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(origin_unix)),
        },
        'config': {
            'threshold':         THRESHOLD,
            'n_consensus':       N_CONSENSUS,
            'consensus_window_s': CONSENSUS_WINDOW,
        },
        'consensus_fired':  consensus_fired,
        'consensus_group':  consensus_group,
        'stations_fired':   fired_stations,
        'stations':         dict(sorted(results.items(),
                                        key=lambda x: x[1].get('dist_km', 99999))),
    }
