import collections
import threading
import time

import numpy as np

from seismic.config import (
    CHANNELS, N_CONSENSUS, CONSENSUS_WINDOW, THRESHOLD, ALERT_COOLDOWN,
    P_LEAD_S, MB_DELAY_S, MAG_MAX_CREDIBLE, LOC_MIN_STA, fmt_mag,
    PER_STATION_COOLDOWN, NOISE_PERSIST_S, MIN_LOGIT_GAP, WIN_SAMPLES,
    STALTA_LARGE_THRESH, THRESHOLD_LARGE,
)
from seismic.localize import locate_epicenter, station_coords
from seismic.model import refine_picks_phasenet
from seismic.runtime import get_threshold
from seismic.state import sensor_state, DetectionSnap
from seismic.station_log import log_station_reading


SUPPRESSED_REPORT_INTERVAL = 60.0


# ── Station ring buffers ───────────────────────────────────────────────────────

station_rings: dict = {}
station_strides: dict = {}
station_status: dict = {}       # key → last status-print time (unix)
station_first_arr: dict = {}    # key → corrected first P-arrival (unix), or None


# ── Consensus state ────────────────────────────────────────────────────────────

class _ConsensusState:
    """Mutable bookkeeping for the consensus engine."""
    recent = collections.deque()
    last_alert: float = 0.0
    sta_last_alert: dict = {}    # key → unix time of last detection
    sta_above_since: dict = {}   # key → unix time station crossed threshold
    suppressed_count: int = 0
    suppressed_last_report: float = 0.0
    rescued_at: dict = {}        # key → unix time of last large-event-rescue qualifying inference


_cs = _ConsensusState()
recent_detections = _cs.recent   # backward-compat alias


# ── Station helpers ────────────────────────────────────────────────────────────

def station_key(net, sta):
    return f"{net}.{sta}"


def init_station_state():
    from seismic.config import ALL_STATIONS
    for net, sta in ALL_STATIONS:
        k = station_key(net, sta)
        station_rings[k] = {ch: collections.deque(maxlen=2000) for ch in CHANNELS}
        station_strides[k] = 0
        station_status[k] = 0.0
        station_first_arr[k] = None


def reset_arrivals():
    for k in station_first_arr:
        station_first_arr[k] = None


# ── Consensus helpers ──────────────────────────────────────────────────────────

def check_consensus(now):
    cutoff = now - CONSENSUS_WINDOW
    pruned = [d for d in _cs.recent if d[0] >= cutoff]
    _cs.recent.clear()
    _cs.recent.extend(pruned)
    stations_fired = {d[1] for d in _cs.recent}
    return len(stations_fired) >= N_CONSENSUS, stations_fired


def _mean_logit_gap(stations_fired):
    gaps = [r[4] for r in _cs.recent if r[1] in stations_fired]
    return float(np.mean(gaps)) if gaps else 0.0


def _mean_magnitude(stations_fired, fallback=0.0):
    mags = [r[3] for r in _cs.recent if r[1] in stations_fired]
    return float(np.mean(mags)) if mags else fallback


def _all_stations_cooled(stations_fired, now):
    return all(
        now - _cs.sta_last_alert.get(k, 0.0) < PER_STATION_COOLDOWN
        for k in stations_fired
    )


def _any_rescued(stations_fired, now):
    """True if any firing station qualified via the large-event rescue path
    recently (within the consensus window). The second-stage veto classifier
    is trained on regional-event data with the same normalization scheme
    that self-suppresses large onsets — it isn't a reliable judge of
    teleseismic events the rescue path exists to catch, so we trust the
    STA/LTA signal instead of subjecting rescued detections to its veto.
    """
    return any(
        now - _cs.rescued_at.get(k, -1e9) <= CONSENSUS_WINDOW
        for k in stations_fired
    )


def _arrival_offsets(p_arrivals, stations_fired, now):
    """Seconds-after-first-arrival per station, capped to CONSENSUS_WINDOW."""
    in_window = {
        k: t for k, t in p_arrivals.items()
        if k in stations_fired and abs(t - now) <= CONSENSUS_WINDOW
    }
    if not in_window:
        return None
    earliest = min(in_window.values())
    return {k: round(t - earliest, 1) for k, t in in_window.items()}


# ── Epicenter localization ─────────────────────────────────────────────────────

def _localize(arrivals, sp_dists):
    """Attempt epicenter localization. Returns ((lat, lon), is_teleseismic) or (None, False)."""
    if len(arrivals) < LOC_MIN_STA:
        n = len(arrivals)
        print(f"  Epicenter:  need {LOC_MIN_STA}+ stations (have {n} P-arrival(s))", flush=True)
        return None, False

    try:
        loc = locate_epicenter(arrivals, sp_distances=sp_dists)
    except Exception as e:
        print(f"  Epicenter:  localization failed ({e})", flush=True)
        return None, False

    if not loc:
        n_known = sum(1 for k, _ in arrivals if k in station_coords)
        print(f"  Epicenter:  need {LOC_MIN_STA} stations w/ coords (have {n_known})", flush=True)
        return None, False

    lat, lon, t0, rms = loc
    is_teleseismic = rms > 15.0
    origin_ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0))
    ns = 'N' if lat >= 0 else 'S'
    ew = 'E' if lon >= 0 else 'W'
    n_used = sum(1 for k, _ in arrivals if k in station_coords)
    tele_tag = '  [TELESEISMIC?]' if is_teleseismic else ''
    print(f"  Epicenter:  {abs(lat):.2f}°{ns} {abs(lon):.2f}°{ew}  "
          f"(rms={rms:.1f}s, {n_used} stations){tele_tag}", flush=True)
    print(f"  Origin:     {origin_ts}  (est.)", flush=True)
    return (lat, lon), is_teleseismic


# ── Detection dispatch ─────────────────────────────────────────────────────────

def _fire_detection(now, ts, conf, logit_gap, stations_fired, mean_gap=0.0):

    if _cs.suppressed_count > 0:
        print(f"  [{ts}] {_cs.suppressed_count} event(s) suppressed "
              f"(saturated magnitude estimate)", flush=True)
        _cs.suppressed_count = 0
        _cs.suppressed_last_report = now

    station_list = ', '.join(sorted(stations_fired))
    print(f"\n{'='*60}", flush=True)
    print(f"  DETECTION  {ts}", flush=True)
    print(f"  Stations:   {station_list}  ({len(stations_fired)}/{N_CONSENSUS} consensus)", flush=True)
    print(f"  Confidence: {conf:.4f}  (threshold={THRESHOLD})  gap={mean_gap:.2f}", flush=True)
    print(f"  Magnitude:  mb computing... (+{MB_DELAY_S:.0f}s)", flush=True)
    print(f"  Lead time:  +{P_LEAD_S}s before P-arrival", flush=True)

    # Only stations that are part of *this* consensus group — station_first_arr
    # is set-once and only cleared by reset_arrivals() below, so a station that
    # tripped threshold long ago on an unrelated signal and never fired can
    # otherwise still be sitting in here and get stitched into this event's
    # localization, corrupting the epicenter/origin-time fit (root cause of
    # false positives correlating with *more* firing stations, not fewer —
    # found 2026-08-25).
    p_arr_snapshot = {
        k: t for k, t in station_first_arr.items()
        if t is not None and k in stations_fired
    }
    refined_p, sp_dists = refine_picks_phasenet(p_arr_snapshot)

    arrivals = [(k, t) for k, t in refined_p.items() if t is not None]
    epicenter, is_teleseismic = _localize(arrivals, sp_dists)

    print(f"{'='*60}\n", flush=True)

    det = DetectionSnap(
        ts=ts,
        unix_ts=now,
        stations=sorted(stations_fired),
        conf=conf,
        logit_gap=logit_gap,
        epicenter=epicenter,
        teleseismic=is_teleseismic if epicenter else False,
        arrival_offsets=_arrival_offsets(p_arr_snapshot, stations_fired, now),
    )
    sensor_state.add_detection(det)

    from seismic.collector import save_detection_window  # noqa: PLC0415
    from seismic.seedlink import _last_stalta            # noqa: PLC0415
    for k in stations_fired:
        save_detection_window(
            station_key=k, unix_ts=now, conf=conf,
            logit_gap=logit_gap,
            stalta_ratio=_last_stalta.get(k, 0.0),
        )

    from seismic.btcvm_anchor import anchor_detection  # noqa: PLC0415
    anchor_detection(det)

    from seismic.otto_bridge import publish_detection   # noqa: PLC0415
    publish_detection(
        p_arrival = min((t for t in p_arr_snapshot.values()), default=now),
        conf      = conf,
        mag_est   = _mean_magnitude(stations_fired),
    )

    from seismic.catalog import report_usgs_deferred  # noqa: PLC0415
    from seismic.magnitude import report_mb_deferred  # noqa: PLC0415

    reset_arrivals()

    threading.Thread(
        target=report_mb_deferred,
        args=(set(stations_fired), refined_p, epicenter, now, ts, conf),
        daemon=True,
    ).start()
    threading.Thread(
        target=report_usgs_deferred,
        args=(now, dict(p_arr_snapshot)),
        daemon=True,
    ).start()


# ── Main inference callback ────────────────────────────────────────────────────

def on_inference(net, sta, conf, mag_est, logit_gap, now, stalta_ratio=0.0):
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    key = station_key(net, sta)
    sensor_state.update_station(key, conf, mag_est)

    # ── Effective threshold: lower it when STA/LTA indicates a large event ────
    # Large teleseismic events (M6.5+) often have lower classifier confidence
    # due to emergent onsets and frequency mismatch. If STA/LTA is very high
    # (strong transient energy), rescue the detection with a lower threshold.
    large_event_rescue = stalta_ratio >= STALTA_LARGE_THRESH
    effective_threshold = THRESHOLD_LARGE if large_event_rescue else get_threshold()
    rescue_tag = ''
    if large_event_rescue and conf >= THRESHOLD_LARGE:
        _cs.rescued_at[key] = now
        rescue_tag = 'rescue+'
        print(f"[{ts}] {key:<8}  LARGE-EVENT RESCUE  conf={conf:.3f}  "
              f"stalta={stalta_ratio:.1f}  (threshold lowered to {THRESHOLD_LARGE})", flush=True)

    # Every inference gets a persisted row regardless of print throttling
    # below — the printed lines are rate-limited for terminal readability,
    # but the diagnostic log needs the full, untruncated per-second record.
    def _log(status):
        log_station_reading(now, key, conf, mag_est, rescue_tag + status, logit_gap, stalta_ratio)

    # ── Below threshold ──────────────────────────────────────────────────────
    if conf < effective_threshold:
        _cs.sta_above_since.pop(key, None)
        if now - station_status[key] > 10.0:
            if mag_est > MAG_MAX_CREDIBLE:
                _cs.suppressed_count += 1
                if now - _cs.suppressed_last_report >= SUPPRESSED_REPORT_INTERVAL:
                    print(f"[{ts}] {_cs.suppressed_count} event(s) suppressed "
                          f"(saturated magnitude estimate)", flush=True)
                    _cs.suppressed_count = 0
                    _cs.suppressed_last_report = now
            else:
                print(f"[{ts}] {key:<8}  conf={conf:.3f}  mag={fmt_mag(mag_est):<5}", flush=True)
            station_status[key] = now
        _log('suppressed' if mag_est > MAG_MAX_CREDIBLE else '')
        return

    # ── Above threshold — check for noise / flatline ─────────────────────────
    if key not in _cs.sta_above_since:
        _cs.sta_above_since[key] = now
    persist_s = now - _cs.sta_above_since[key]

    if persist_s > NOISE_PERSIST_S:
        if now - station_status[key] > 30.0:
            print(f"[{ts}] {key:<10} NOISY persist={persist_s:.0f}s conf={conf:.3f} "
                  f"— excluded from consensus", flush=True)
            station_status[key] = now
        sensor_state.update_station(key, conf, mag_est)
        _log('noisy')
        return

    if sensor_state.is_flatline(key):
        if now - station_status[key] > 60.0:
            print(f"[{ts}] {key:<10} FLATLINE conf={conf:.3f} — excluded from consensus",
                  flush=True)
            station_status[key] = now
        sensor_state.update_station(key, conf, mag_est)
        _log('flatline')
        return

    # ── Record P-arrival and check consensus ─────────────────────────────────
    if station_first_arr[key] is None:
        station_first_arr[key] = now + P_LEAD_S

    _cs.recent.append((now, key, conf, mag_est, logit_gap))
    consensus_met, stations_fired = check_consensus(now)

    if not consensus_met:
        waiting = N_CONSENSUS - len(stations_fired)
        print(f"  [{ts}] {key} CANDIDATE conf={conf:.3f} mag={fmt_mag(mag_est)} "
              f"(waiting for {waiting} more station(s))", flush=True)
        _log('candidate')
        return

    # ── Gate checks before firing ─────────────────────────────────────────────
    cooldown_ok = now - _cs.last_alert > ALERT_COOLDOWN
    mean_gap    = _mean_logit_gap(stations_fired)
    gap_ok      = mean_gap >= MIN_LOGIT_GAP
    all_cooled  = _all_stations_cooled(stations_fired, now)

    if not cooldown_ok or not gap_ok or all_cooled:
        print(f"  [{ts}] consensus met but gated: "
              f"cooldown_ok={cooldown_ok} gap_ok={gap_ok} (gap={mean_gap:.2f} "
              f"min={MIN_LOGIT_GAP}) all_cooled={all_cooled}", flush=True)
        _log('gated')
        return

    # ── Second-stage classifier veto ─────────────────────────────────────────
    # Skipped for large-event rescues: the veto classifier is trained on
    # regional-event data normalized the same self-suppressing way the
    # rescue path was built to work around, so it isn't a fair judge here.
    if _any_rescued(stations_fired, now):
        print(f"  [{ts}] classifier veto skipped (large-event rescue)", flush=True)
    else:
        from seismic import classifier as _clf  # noqa: PLC0415
        clf_prob, clf_suppress = _clf.score(stations_fired, station_rings, CHANNELS, WIN_SAMPLES)
        if clf_suppress:
            print(f"  [{ts}] VETOED by classifier (prob={clf_prob:.3f})", flush=True)
            _log('vetoed')
            return
        if clf_prob is not None:
            print(f"  [{ts}] classifier OK (prob={clf_prob:.3f})", flush=True)

    # ── Fire ──────────────────────────────────────────────────────────────────
    _cs.last_alert = now
    for k in stations_fired:
        _cs.sta_last_alert[k] = now

    _cs.recent.clear()
    _log('fired')
    _fire_detection(now, ts, conf, logit_gap, stations_fired, mean_gap)
