import collections
import threading
import time

import numpy as np

from seismic.config import (
    CHANNELS, N_CONSENSUS, CONSENSUS_WINDOW, THRESHOLD, ALERT_COOLDOWN,
    P_LEAD_S, MB_DELAY_S, MAG_MAX_CREDIBLE, LOC_MIN_STA, fmt_mag,
    PER_STATION_COOLDOWN,
)
from seismic.runtime import get_threshold
from seismic.localize import locate_epicenter, station_coords
from seismic.model import refine_picks_phasenet
from seismic.state import sensor_state, DetectionSnap

# ── Multi-station consensus state ─────────────────────────────────────────────
station_rings = {}   # key → {ch: deque}
station_strides = {}   # key → int
station_status = {}   # key → float (last status print time)
station_first_arr = {}   # key → float or None (first P-arrival timestamp, corrected)

recent_detections = collections.deque()
last_alert = [0.0]
_station_last_alert: dict = {}  # key → float (unix time of last alert involving this station)
suppressed_mag_count = [0]
suppressed_mag_last_report = [0.0]
SUPPRESSED_REPORT_INTERVAL = 60.0


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


def check_consensus(now):
    cutoff = now - CONSENSUS_WINDOW
    pruned = [d for d in recent_detections if d[0] >= cutoff]
    recent_detections.clear()
    recent_detections.extend(pruned)
    stations_fired = set(d[1] for d in recent_detections)
    return len(stations_fired) >= N_CONSENSUS, stations_fired


def on_inference(net, sta, conf, mag_est, logit_gap, now):
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    key = station_key(net, sta)
    sensor_state.update_station(key, conf, mag_est)

    if conf >= get_threshold():
        # Record first P-wave arrival (corrected for model pre-P horizon)
        if station_first_arr[key] is None:
            station_first_arr[key] = now + P_LEAD_S

        recent_detections.append((now, key, conf, mag_est))
        consensus_met, stations_fired = check_consensus(now)

        # Per-station cooldown: skip if *all* newly-fired stations have alerted recently
        _sta_cooled = all(
            now - _station_last_alert.get(k, 0.0) < PER_STATION_COOLDOWN
            for k in stations_fired
        )
        if consensus_met and now - last_alert[0] > ALERT_COOLDOWN and not _sta_cooled:
            last_alert[0] = now
            for _k in stations_fired:
                _station_last_alert[_k] = now
            mag_consensus = (
                float(np.mean([r[3] for r in recent_detections if r[1] in stations_fired]))
                if recent_detections else mag_est
            )
            recent_detections.clear()

            # Snapshot arrivals before reset (needed for deferred mb thread)
            p_arr_snapshot = {k: t for k, t in station_first_arr.items() if t is not None}
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

            # Refine P picks and derive S-P distances via PhaseNet
            refined_p, sp_dists = refine_picks_phasenet(p_arr_snapshot)

            # Attempt epicenter localization
            is_teleseismic = False
            arrivals = [(k, t) for k, t in refined_p.items() if t is not None]
            if len(arrivals) >= LOC_MIN_STA:
                try:
                    loc = locate_epicenter(arrivals, sp_distances=sp_dists)
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

            from seismic.btcvm_anchor import anchor_detection  # noqa: PLC0415
            anchor_detection(det_rec)

            from seismic.catalog import report_usgs_deferred  # noqa: PLC0415
            from seismic.magnitude import report_mb_deferred  # noqa: PLC0415

            reset_arrivals()

            # Launch deferred mb (fires Slack alert after magnitude is known)
            threading.Thread(
                target=report_mb_deferred,
                args=(set(stations_fired), refined_p, epicenter_latlon, now, ts, conf),
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
                print(f"[{ts}] {key:<8}  conf={conf:.3f}  mag={fmt_mag(mag_est):<5}", flush=True)
            station_status[key] = now
