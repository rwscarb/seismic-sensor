import json
import os
import time

from seismic.config import (
    USGS_SIG_MIN_MAG, USGS_POLL_INTERVAL, TELE_MATCH_WINDOW, SLACK_WEBHOOK_URL, APP_URL,
)
from seismic.localize import station_coords, haversine_km, p_travel_time_s
from seismic.state import sensor_state  # noqa: F401 — used for update_usgs override

# ── USGS significant-event watcher ───────────────────────────────────────────
# Polls USGS every USGS_POLL_INTERVAL seconds.  For each significant event
# (M ≥ USGS_SIG_MIN_MAG) not yet seen, computes expected P-wave arrival time
# at our stations and checks whether sensor_state.detections contains a match.
# Logs [DETECTED] or [MISSED] and optionally Slacks the result.

_SIG_SEEN_PATH = os.environ.get('SIG_SEEN_PATH', '/data/sig_seen.json')

def _load_sig_seen() -> set:
    try:
        with open(_SIG_SEEN_PATH) as f:
            return set(json.load(f))
    except (FileNotFoundError, Exception):
        return set()

def _save_sig_seen(seen: set) -> None:
    try:
        tmp = _SIG_SEEN_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(list(seen), f)
        os.replace(tmp, _SIG_SEEN_PATH)
    except Exception as e:
        print(f'  [sig-watch] could not save seen set: {e}', flush=True)

_sig_seen: set = _load_sig_seen()   # event IDs already processed (persisted across restarts)


def _expected_p_arrival(event_lat, event_lon, event_unix):
    """Return the earliest expected P-wave arrival time (unix) across all stations
    with known coordinates.  Falls back to event_unix + 600s if no coords yet."""
    arrivals = []
    for key, (sta_lat, sta_lon) in station_coords.items():
        dist_km = haversine_km(event_lat, event_lon, sta_lat, sta_lon)
        arrivals.append(event_unix + p_travel_time_s(dist_km))
    return min(arrivals) if arrivals else event_unix + 600.0


def _find_matching_detection(expected_arrival):
    """Return the closest DetectionSnap within TELE_MATCH_WINDOW of expected_arrival, or None."""
    best = None
    best_diff = TELE_MATCH_WINDOW
    with sensor_state._lock:
        for det in sensor_state.detections:
            diff = abs(det.unix_ts - expected_arrival)
            if diff < best_diff:
                best_diff = diff
                best = det
    return best


def _slack_sig_event(event, expected_arrival, matched_det):
    """Post a significant-event Slack alert (distinct from detection alerts)."""
    from seismic.runtime import is_muted  # noqa: PLC0415
    if is_muted():
        print('  [sig-watch] alerts muted — skipping significant-event alert', flush=True)
        return
    if not SLACK_WEBHOOK_URL:
        return
    import urllib.request
    mag = event['mag']
    place = event['place']
    eid = event.get('event_id', '')
    t_str = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(event['time']))
    arr_str = time.strftime('%H:%M:%SZ', time.gmtime(expected_arrival))
    if matched_det:
        lag = abs(matched_det.unix_ts - expected_arrival)
        det_ts = int(matched_det.unix_ts)
        if APP_URL:
            det_url = f'{APP_URL}/?det={det_ts}'
            status = f'✅ <{det_url}|DETECTED> (±{lag:.0f}s, conf {matched_det.conf:.3f})'
        else:
            status = f'✅ DETECTED (±{lag:.0f}s, conf {matched_det.conf:.3f})'
    else:
        status = '❌ NOT DETECTED in log'
    usgs_url = f'https://earthquake.usgs.gov/earthquakes/eventpage/{eid}/executive' if eid else ''
    link_line = f'\n<{usgs_url}|View on USGS>' if usgs_url else ''
    text = (
        f'🌍 *Significant Earthquake — M{mag}*\n'
        f'Location: `{place}`\n'
        f'Origin: `{t_str}`\n'
        f'Expected P at sensor: `{arr_str}`\n'
        f'Sensor status: {status}'
        f'{link_line}'
    )
    payload = json.dumps({'text': text, 'mrkdwn': True}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f'  [sig-watch] slack failed: {e}', flush=True)


def compute_recall_window(days: float = 7.0, minmag: float = 4.5) -> dict:
    """Query USGS for all events in the past `days` days with mag >= minmag
    and check each against the detection log.

    Returns a confusion-matrix dict:
      true_positives  : USGS events we detected
      false_negatives : USGS events we missed
      events          : list of per-event dicts with outcome
    """
    import urllib.request
    end_ts = time.time()
    start_ts = end_ts - days * 86400
    url = (
        f'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
        f'&minmagnitude={minmag}'
        f'&orderby=time-asc'
        f'&starttime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start_ts))}'
        f'&endtime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end_ts))}'
        f'&limit=200'
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return {'error': str(e)}

    true_positives, false_negatives = 0, 0
    events = []
    for feat in data.get('features', []):
        p = feat['properties']
        c = feat['geometry']['coordinates']
        mag = p.get('mag')
        if mag is None or mag < minmag:
            continue
        event_unix = p['time'] / 1000
        exp_arr = _expected_p_arrival(c[1], c[0], event_unix)
        matched = _find_matching_detection(exp_arr)
        detected = matched is not None
        row = {
            'usgs_id':    feat.get('id', ''),
            'mag':        mag,
            'place':      p.get('place', '?'),
            'time':       time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(event_unix)),
            'lat':        round(c[1], 3),
            'lon':        round(c[0], 3),
            'depth_km':   round(c[2], 1) if len(c) > 2 else None,
            'detected':   detected,
        }
        if detected:
            row['det_conf'] = round(matched.conf, 3)
            row['det_lag_s'] = round(abs(matched.unix_ts - exp_arr), 1)
            true_positives += 1
        else:
            false_negatives += 1
        events.append(row)

    total = true_positives + false_negatives
    recall = round(true_positives / total, 4) if total else None
    return {
        'days':             days,
        'minmag':           minmag,
        'usgs_events':      total,
        'true_positives':   true_positives,
        'false_negatives':  false_negatives,
        'recall':           recall,
        'events':           sorted(events, key=lambda x: x['time'], reverse=True),
    }


def poll_usgs_significant():
    """Background thread: poll USGS significant-event feed and cross-check detections."""
    import urllib.request
    # Wait a bit after startup so station_coords has time to populate
    time.sleep(30)
    print('[sig-watch] USGS significant-event watcher started', flush=True)
    while True:
        try:
            url = (
                f'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
                f'&minmagnitude={USGS_SIG_MIN_MAG}'
                f'&orderby=time-asc'
                f'&starttime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))}'
                f'&endtime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}'
                f'&limit=50'
            )
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read())
            for feat in data.get('features', []):
                eid = feat.get('id', '')
                if eid in _sig_seen:
                    continue
                _sig_seen.add(eid)
                _save_sig_seen(_sig_seen)
                p = feat['properties']
                c = feat['geometry']['coordinates']
                mag = p.get('mag', 0)
                if mag is None or mag < USGS_SIG_MIN_MAG:
                    continue
                event = {
                    'mag': mag,
                    'place': p.get('place', '?'),
                    'time': p['time'] / 1000,
                    'lat': c[1],
                    'lon': c[0],
                    'depth': c[2] if len(c) > 2 else 0,
                    'event_id': eid,
                }
                t_str = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(event['time']))
                exp_arr = _expected_p_arrival(event['lat'], event['lon'], event['time'])
                arr_str = time.strftime('%H:%MZ', time.gmtime(exp_arr))
                matched = _find_matching_detection(exp_arr)
                ns = 'N' if event['lat'] >= 0 else 'S'
                ew = 'E' if event['lon'] >= 0 else 'W'
                coord_str = f"{abs(event['lat']):.2f}°{ns} {abs(event['lon']):.2f}°{ew}"
                if matched:
                    lag = abs(matched.unix_ts - exp_arr)
                    status = f'DETECTED (Δ={lag:.0f}s conf={matched.conf:.3f})'
                    # Authoritative USGS sig-event coords override per-detection catalog pick
                    usgs_record = {**event, 'source': 'usgs', 'magType': 'M'}
                    sensor_state.update_usgs(matched.unix_ts, usgs_record)
                    print(f'  [sig-watch] updated detection {matched.unix_ts:.0f} '
                          f'with USGS coords {coord_str}', flush=True)
                else:
                    status = 'NOT DETECTED'
                print(
                    f'  [sig-watch {t_str}] M{mag} {event["place"]} {coord_str} '
                    f'depth={event["depth"]:.0f}km → P@{arr_str} → {status}',
                    flush=True,
                )
                _slack_sig_event(event, exp_arr, matched)
        except Exception as e:
            print(f'  [sig-watch] poll error: {e}', flush=True)
        time.sleep(USGS_POLL_INTERVAL)
