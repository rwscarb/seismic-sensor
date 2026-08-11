"""
Early Warning Engine (PoC)

After a confirmed USGS/EMSC location, compute S-wave arrival windows for
monitored cities and fire a Slack alert for those still in the warning window.

Realistic use case: large teleseismic events (M6.5+) where S-wave travel time
to distant cities (2000–8000 km) exceeds our detection latency.  For regional
events our detection arrives after local S-waves; the alert honestly reflects
that with negative warning times.
"""

import json
import math
import time

from seismic.localize import haversine_km

# ── Constants ─────────────────────────────────────────────────────────────────
S_VEL_KMS   = 3.5      # average crustal S-wave velocity (km/s)
EEW_MIN_MAG = 4.5      # only fire EEW for M4.5+
EEW_MAX_DIST_KM = 8000 # city search radius
EEW_WINDOW_S    = 3600 # include cities with S arriving within this many seconds
EEW_PAST_S      = 300  # also show cities impacted up to 5 min before alert fires

# ── Monitored city list ───────────────────────────────────────────────────────
# (name, lat, lon, metro_population)
CITIES = [
    # Mediterranean / Southern Europe
    ('Athens',        37.98,  23.73, 3_200_000),
    ('Istanbul',      41.01,  28.95, 15_400_000),
    ('Ankara',        39.93,  32.85,  5_600_000),
    ('Izmir',         38.42,  27.14,  3_000_000),
    ('Thessaloniki',  40.64,  22.94,    820_000),
    ('Rome',          41.90,  12.49,  4_300_000),
    ('Naples',        40.85,  14.27,  3_100_000),
    ('Palermo',       38.12,  13.36,    680_000),
    ('Cairo',         30.06,  31.25, 20_000_000),
    ('Alexandria',    31.20,  29.92,  5_200_000),
    ('Beirut',        33.89,  35.50,  2_200_000),
    ('Tel Aviv',      32.08,  34.78,  4_200_000),
    ('Damascus',      33.51,  36.29,  2_500_000),
    ('Amman',         31.96,  35.95,  2_200_000),
    ('Nicosia',       35.17,  33.36,    330_000),
    ('Tunis',         36.82,  10.17,  2_700_000),
    ('Tripoli',       32.89,  13.18,  1_200_000),
    ('Algiers',       36.74,   3.06,  3_900_000),
    ('Casablanca',    33.59,  -7.62,  3_900_000),
    # Central / Eastern Europe
    ('Sofia',         42.70,  23.32,  1_300_000),
    ('Bucharest',     44.43,  26.10,  2_100_000),
    ('Belgrade',      44.80,  20.46,  1_700_000),
    ('Budapest',      47.50,  19.04,  2_900_000),
    ('Zagreb',        45.81,  15.98,    800_000),
    ('Vienna',        48.21,  16.37,  1_900_000),
    ('Warsaw',        52.23,  21.01,  1_800_000),
    ('Kyiv',          50.45,  30.52,  3_600_000),
    ('Moscow',        55.75,  37.62, 12_500_000),
    # Middle East / Central Asia
    ('Baghdad',       33.34,  44.40,  7_200_000),
    ('Tehran',        35.69,  51.39,  9_200_000),
    ('Riyadh',        24.69,  46.72,  7_600_000),
    ('Dubai',         25.20,  55.27,  3_300_000),
    ('Karachi',       24.86,  67.01, 16_100_000),
    ('Lahore',        31.55,  74.34, 13_100_000),
    ('Delhi',         28.61,  77.21, 32_900_000),
    ('Mumbai',        19.08,  72.88, 20_700_000),
    # East Asia
    ('Beijing',       39.91, 116.39, 21_500_000),
    ('Shanghai',      31.23, 121.47, 24_900_000),
    ('Tokyo',         35.69, 139.69, 37_400_000),
    ('Osaka',         34.69, 135.50, 19_200_000),
    ('Seoul',         37.57, 126.98, 25_500_000),
    ('Manila',        14.60, 120.98, 14_000_000),
    ('Taipei',        25.04, 121.56,  7_100_000),
    ('Jakarta',       -6.21, 106.85, 10_600_000),
    ('Bangkok',       13.75, 100.52, 10_700_000),
    ('Singapore',      1.35, 103.82,  5_900_000),
    # Pacific / Oceania
    ('Sydney',       -33.87, 151.21,  5_300_000),
    ('Melbourne',    -37.81, 144.96,  5_100_000),
    ('Auckland',     -36.87, 174.77,  1_700_000),
    ('Honolulu',      21.31,-157.85,  1_000_000),
    ('Anchorage',     61.22,-149.90,    400_000),
    # Americas — West Coast
    ('Vancouver',     49.25,-123.12,  2_600_000),
    ('Seattle',       47.61,-122.33,  3_900_000),
    ('Portland',      45.52,-122.68,  2_500_000),
    ('San Francisco', 37.77,-122.42,  4_700_000),
    ('Los Angeles',   34.05,-118.24, 13_200_000),
    ('San Diego',     32.72,-117.15,  3_300_000),
    ('Tijuana',       32.52,-117.03,  2_000_000),
    ('Lima',         -12.05, -77.04, 10_900_000),
    ('Santiago',     -33.45, -70.67,  7_100_000),
    # Americas — East / Central
    ('New York',      40.71, -74.01, 18_800_000),
    ('Chicago',       41.88, -87.63,  9_500_000),
    ('Houston',       29.76, -95.37,  7_100_000),
    ('Miami',         25.77, -80.20,  6_200_000),
    ('Mexico City',   19.43, -99.13, 21_600_000),
    ('Bogota',         4.71, -74.07, 10_800_000),
    ('Buenos Aires', -34.60, -58.38, 15_300_000),
    ('São Paulo',    -23.55, -46.63, 22_400_000),
    ('Rio de Janeiro',-22.91,-43.17,  7_700_000),
    # Africa
    ('Lagos',          6.46,   3.38, 15_300_000),
    ('Kinshasa',      -4.32,  15.32, 15_600_000),
    ('Nairobi',       -1.29,  36.82,  4_400_000),
    ('Johannesburg', -26.20,  28.04, 10_000_000),
    ('Addis Ababa',    9.02,  38.75,  5_000_000),
    # Western Europe
    ('London',        51.51,  -0.13, 14_800_000),
    ('Paris',         48.86,   2.35, 12_300_000),
    ('Madrid',        40.42,  -3.70,  6_600_000),
    ('Lisbon',        38.72,  -9.14,  2_800_000),
    ('Berlin',        52.52,  13.41,  3_600_000),
    ('Amsterdam',     52.37,   4.90,  1_200_000),
    ('Brussels',      50.85,   4.35,  2_100_000),
    ('Zurich',        47.38,   8.54,  1_400_000),
]


# ── Core computation ──────────────────────────────────────────────────────────

def _mmi_estimate(mag, dist_km):
    """
    Rough Modified Mercalli Intensity estimate (Wald et al. 1999 approximation).
    Returns (roman_numeral_str, description).
    """
    if dist_km < 1:
        dist_km = 1
    mmi = 2.20 * mag - 1.46 * math.log10(dist_km) - 1.05
    mmi = max(0.5, min(10.0, mmi))
    if mmi >= 9: return 'IX–X', 'violent'
    if mmi >= 8: return 'VIII', 'severe'
    if mmi >= 7: return 'VII', 'very strong'
    if mmi >= 6: return 'VI', 'strong'
    if mmi >= 5: return 'V', 'moderate'
    if mmi >= 4: return 'IV', 'light'
    if mmi >= 3: return 'III', 'weak'
    if mmi >= 2: return 'II', 'faint'
    return 'I', 'imperceptible'


def compute_warnings(origin_lat, origin_lon, origin_unix, fire_unix, mag):
    """
    For each monitored city compute S-wave arrival and warning window.

    Returns list of dicts sorted by distance:
      {name, dist_km, s_arrival_unix, warning_s, mmi_str, mmi_desc, pop}
    Only includes cities within EEW_MAX_DIST_KM where the S-wave hasn't
    already passed more than EEW_PAST_S seconds ago.
    """
    now = fire_unix
    results = []
    seen = set()
    for name, c_lat, c_lon, pop in CITIES:
        if name in seen:
            continue
        seen.add(name)
        dist_km = haversine_km(origin_lat, origin_lon, c_lat, c_lon)
        if dist_km > EEW_MAX_DIST_KM:
            continue
        s_travel = dist_km / S_VEL_KMS
        s_arrival = origin_unix + s_travel
        warning_s = s_arrival - now
        if warning_s < -EEW_PAST_S:
            continue
        mmi_str, mmi_desc = _mmi_estimate(mag, dist_km)
        results.append(dict(
            name=name,
            dist_km=dist_km,
            s_arrival_unix=s_arrival,
            warning_s=warning_s,
            mmi_str=mmi_str,
            mmi_desc=mmi_desc,
            pop=pop,
        ))
    results.sort(key=lambda x: x['dist_km'])
    return results


# ── Alert formatting & delivery ───────────────────────────────────────────────

def fire_eew_alert(det_unix, catalog_event):
    """
    Call after a USGS/EMSC location is confirmed.
    Sends an EEW Slack alert if there are cities in the warning window.
    """
    from seismic.config import SLACK_WEBHOOK_URL, APP_URL  # noqa: PLC0415

    if not SLACK_WEBHOOK_URL:
        return
    if catalog_event is None:
        return

    mag = catalog_event.get('mag') or 0
    if mag < EEW_MIN_MAG:
        print(f"  [eew] M{mag} below threshold ({EEW_MIN_MAG}), skipping", flush=True)
        return

    origin_lat  = catalog_event['lat']
    origin_lon  = catalog_event['lon']
    origin_unix = catalog_event['time']
    place       = catalog_event.get('place', 'unknown region')
    mag_type    = catalog_event.get('magType', '')
    source      = (catalog_event.get('source') or 'usgs').upper()
    event_id    = catalog_event.get('event_id', '')
    depth       = catalog_event.get('depth', 0) or 0

    now = time.time()
    warnings = compute_warnings(origin_lat, origin_lon, origin_unix, now, mag)

    if not warnings:
        print(f"  [eew] M{mag} {place}: no cities in range/window", flush=True)
        return

    future  = [w for w in warnings if w['warning_s'] > 0]
    current = [w for w in warnings if -30 <= w['warning_s'] <= 0]
    past    = [w for w in warnings if w['warning_s'] < -30]

    # Anchor URL
    if event_id and source == 'USGS':
        cat_url = (f"https://earthquake.usgs.gov/earthquakes/eventpage/"
                   f"{event_id}/executive")
    elif event_id and source == 'EMSC':
        cat_url = (f"https://www.seismicportal.eu/eventdetails.html?"
                   f"unid={event_id}")
    else:
        cat_url = APP_URL or 'https://seismic.fib896.com'

    dash_url = APP_URL or 'https://seismic.fib896.com'
    ns = 'N' if origin_lat >= 0 else 'S'
    ew = 'E' if origin_lon >= 0 else 'W'
    coord_str = f"{abs(origin_lat):.2f}°{ns} {abs(origin_lon):.2f}°{ew}"

    lines = [
        f'🚨 *EEW — M{mag}{mag_type} {place}* ({source})',
        f'`{coord_str}` · depth {depth:.0f} km',
        '',
    ]

    if future:
        lines.append('*S-wave arrivals incoming:*')
        for w in future[:10]:
            mins, secs = divmod(int(w['warning_s']), 60)
            t_str = (f"{mins}m {secs:02d}s" if mins else f"{secs}s")
            icon = ('🔴' if w['warning_s'] < 60 else
                    '🟠' if w['warning_s'] < 300 else '🟡')
            lines.append(
                f"  {icon} *{w['name']}* — _{t_str}_ · "
                f"{w['dist_km']:.0f} km · MMI {w['mmi_str']} ({w['mmi_desc']})"
            )

    if current:
        lines.append('*Arriving now:*')
        for w in current:
            lines.append(
                f"  🔴 *{w['name']}* · MMI {w['mmi_str']} ({w['mmi_desc']})"
            )

    if past:
        lines.append(f'*Already impacted* (last {EEW_PAST_S//60} min):')
        for w in past[:8]:
            ago = int(-w['warning_s'])
            m, s = divmod(ago, 60)
            t_str = f"{m}m {s:02d}s ago" if m else f"{s}s ago"
            lines.append(
                f"  ⚫ *{w['name']}* — {t_str} · "
                f"MMI {w['mmi_str']} ({w['mmi_desc']})"
            )

    if not future and not current:
        # All cities already hit — still useful to know
        lines.append('_S-waves have already passed monitored cities in range._')

    lines += [
        '',
        f'<{cat_url}|{source} event> · <{dash_url}|Dashboard>',
    ]

    text = '\n'.join(lines)
    _post_slack(SLACK_WEBHOOK_URL, text)
    n_future = len(future)
    n_past   = len(past) + len(current)
    print(f"  [eew] alert sent — M{mag} {place} · "
          f"{n_future} cities with warning, {n_past} already impacted", flush=True)


def _post_slack(webhook_url, text):
    import urllib.request  # noqa: PLC0415
    payload = json.dumps({'text': text, 'mrkdwn': True}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  [eew] webhook failed: {e}", flush=True)
