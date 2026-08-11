import json
import os
import time

from seismic.config import (
    USGS_MIN_MAG, EMSC_MIN_MAG, SLACK_WEBHOOK_URL, fmt_mag,
)
from seismic.localize import station_coords, haversine_km, p_travel_time_s
from seismic.state import sensor_state

CAL_LOG = os.environ.get('CAL_LOG', '/data/seismic_cal.jsonl')


def _score_catalog_event(feat_lat, feat_lon, feat_origin_unix, p_arrivals):
    """
    Mean absolute residual (seconds) between observed P-arrivals and the
    arrivals predicted by this candidate event using each station's known
    coordinates and P_VEL_KM_S.  Stations without known coords are skipped.
    Falls back to absolute difference between origin time and earliest P minus
    a 600 s nominal travel time when no station coordinates are available.
    Lower score = better match.
    """
    residuals = []
    for sta, obs_t in p_arrivals.items():
        if sta not in station_coords:
            continue
        sta_lat, sta_lon = station_coords[sta]
        dist_km = haversine_km(feat_lat, feat_lon, sta_lat, sta_lon)
        expected_t = feat_origin_unix + p_travel_time_s(dist_km)
        residuals.append(abs(obs_t - expected_t))
    if residuals:
        return sum(residuals) / len(residuals)
    # Fallback: score by deviation from a 600 s nominal travel time
    min_arr = min(p_arrivals.values()) if p_arrivals else feat_origin_unix + 600
    return abs((min_arr - feat_origin_unix) - 600)


def query_usgs_event(det_unix, p_arrivals):
    """
    Search USGS ComCat for earthquakes that could explain this detection.
    Searches the window [earliest_P - 2400s, earliest_P - 30s] (up to 40 min before P).
    Returns a dict with mag/place/time or None.
    """
    import urllib.request
    min_arr = min(p_arrivals.values()) if p_arrivals else det_unix
    t0 = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(min_arr - 2400))
    t1 = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(min_arr - 30))
    url = (
        f'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
        f'&starttime={t0}&endtime={t1}'
        f'&minmagnitude={USGS_MIN_MAG}&orderby=magnitude-desc&limit=10'
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        feats = data.get('features', [])
        if not feats:
            return None

        # Pick the candidate whose predicted P-arrivals best match observations
        def score(feat):
            c = feat['geometry']['coordinates']
            return _score_catalog_event(c[1], c[0], feat['properties']['time'] / 1000, p_arrivals)

        f = min(feats, key=score)
        coords = f['geometry']['coordinates']
        p = f['properties']
        return {
            'mag': p.get('mag'),
            'magType': p.get('magType', '?'),
            'place': p.get('place', '?'),
            'time': p['time'] / 1000,
            'lat': coords[1],
            'lon': coords[0],
            'depth': coords[2],
            'event_id': f.get('id', ''),
        }
    except Exception:
        return None


def send_slack_alert(ts, stations_fired, conf, epicenter=None, mag_est=None):
    """POST a detection alert to Slack webhook if configured."""
    if not SLACK_WEBHOOK_URL:
        return
    import urllib.request
    sta_list = ' · '.join(sorted(stations_fired))
    epi_str = f'\nEpicenter: `{epicenter[0]:.2f}N {epicenter[1]:.2f}E`' if epicenter else ''
    mag_str = f'\nMagnitude: `mb {mag_est:.1f}` _(IASPEI est.)_' if mag_est is not None else ''
    text = (
        f'🌍 *Seismic Detection*\n'
        f'Time: `{ts}`\n'
        f'Stations: `{sta_list}`\n'
        f'Confidence: `{conf:.3f}`{mag_str}{epi_str}\n'
        f'<https://seismic-sensor.fly.dev|View dashboard>'
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
        print(f"  [slack] webhook failed: {e}", flush=True)


def query_emsc_event(det_unix, p_arrivals):
    """
    EMSC fallback: European-Mediterranean catalog, lower magnitude threshold.
    Bounding box covers Europe + Mediterranean to reduce irrelevant global matches.
    """
    import urllib.request
    min_arr = min(p_arrivals.values()) if p_arrivals else det_unix
    t0 = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(min_arr - 2400))
    t1 = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(min_arr - 30))
    url = (
        f'https://www.seismicportal.eu/fdsnws/event/1/query?format=json'
        f'&starttime={t0}&endtime={t1}'
        f'&minmagnitude={EMSC_MIN_MAG}&orderby=magnitude-desc&limit=10'
        f'&minlatitude=25&maxlatitude=75&minlongitude=-30&maxlongitude=60'
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        feats = data.get('features', [])
        if not feats:
            return None

        def score(feat):
            c = feat['geometry']['coordinates']
            origin = feat['properties'].get('time')
            origin_unix = origin / 1000 if isinstance(origin, (int, float)) else 0
            return _score_catalog_event(c[1], c[0], origin_unix, p_arrivals)

        f = min(feats, key=score)
        coords = f['geometry']['coordinates']
        p = f['properties']
        return {
            'mag': p.get('mag') or p.get('magnitude'),
            'magType': p.get('magtype') or p.get('magnitudetype', '?'),
            'place': p.get('flynn_region') or p.get('region', '?'),
            'time': p['time'] / 1000 if isinstance(p.get('time'), (int, float)) else 0,
            'lat': coords[1],
            'lon': coords[0],
            'depth': coords[2] if len(coords) > 2 else 0,
            'source': 'emsc',
            'event_id': p.get('unid') or f.get('id', ''),
        }
    except Exception:
        return None


def _append_calibration(det_unix, catalog_event):
    """Append a confirmed detection→catalog pair to the calibration log."""
    det = sensor_state.get_detection(det_unix)
    if det is None or catalog_event is None:
        return
    rec = {
        'det_unix': det_unix,
        'det_lat': det.epicenter[0] if det.epicenter else None,
        'det_lon': det.epicenter[1] if det.epicenter else None,
        'det_mb': det.mb,
        'cat_lat': catalog_event['lat'],
        'cat_lon': catalog_event['lon'],
        'cat_mag': catalog_event['mag'],
        'cat_depth': catalog_event.get('depth'),
        'cat_source': catalog_event.get('source', 'usgs'),
    }
    try:
        os.makedirs(os.path.dirname(CAL_LOG), exist_ok=True)
        with open(CAL_LOG, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        print(f"  [cal] logged calibration record ({CAL_LOG})", flush=True)
    except Exception as e:
        print(f"  [cal] write failed: {e}", flush=True)


def report_usgs_deferred(det_unix, p_arrivals):
    """Thread: queries USGS ~10s after detection, then EMSC if no match."""
    time.sleep(10)
    event = query_usgs_event(det_unix, p_arrivals)
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    if event:
        event['source'] = 'usgs'
        ns = 'N' if event['lat'] >= 0 else 'S'
        ew = 'E' if event['lon'] >= 0 else 'W'
        print(f"  [usgs {ts}] M{event['mag']}{event['magType']} — {event['place']}", flush=True)
        print(f"  [usgs {ts}] {abs(event['lat']):.2f}°{ns} {abs(event['lon']):.2f}°{ew}  "
              f"depth={event['depth']:.0f}km", flush=True)
        sensor_state.update_usgs(det_unix, event)
        _append_calibration(det_unix, event)
        return
    print(f"  [usgs {ts}] no USGS match (M{USGS_MIN_MAG}+ in window) — trying EMSC", flush=True)
    event = query_emsc_event(det_unix, p_arrivals)
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    if event:
        ns = 'N' if event['lat'] >= 0 else 'S'
        ew = 'E' if event['lon'] >= 0 else 'W'
        print(f"  [emsc {ts}] M{event['mag']}{event['magType']} — {event['place']}", flush=True)
        print(f"  [emsc {ts}] {abs(event['lat']):.2f}°{ns} {abs(event['lon']):.2f}°{ew}  "
              f"depth={event['depth']:.0f}km", flush=True)
        sensor_state.update_usgs(det_unix, event)
        _append_calibration(det_unix, event)
    else:
        print(f"  [emsc {ts}] no EMSC match (M{EMSC_MIN_MAG}+ European window)", flush=True)
        sensor_state.update_usgs(det_unix, None)
