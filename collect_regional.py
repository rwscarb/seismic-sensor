#!/usr/bin/env python3
"""
Collect regional P-wave training windows from IRIS for Pacific/Australian stations.
Targets significant events (M5.5+) at 12–55° epicentral distance from each station,
to give the model exposure to waveforms it was never trained on.

Output: npz files in ./training/ compatible with train.py format.
"""
import time, json, math, os, sys, urllib.request
import numpy as np
from obspy import UTCDateTime
from obspy.clients.fdsn import Client

# ── Station coords (lat, lon) ─────────────────────────────────────────────────
STATIONS = {
    'IU.NWAO': (-32.928,  117.239),   # Narrogin, W. Australia
    'II.WRAB': (-19.934,  134.360),   # Warramunga, N. Territory
    'IU.MAJO': ( 36.540,  138.207),   # Matsushiro, Japan
    'IU.SNZO': (-41.310,  174.705),   # South Karori, New Zealand
}

TARGET_DIST_DEG  = (12.0, 55.0)
MIN_MAG          = 5.5
WIN_SAMPLES      = 100
TARGET_SRATE     = 100.0
WIN_S            = WIN_SAMPLES / TARGET_SRATE   # 1.0 s
P_LEAD_S         = 0.4                           # samples before P

# ── IASP91 table ──────────────────────────────────────────────────────────────
_DEG = [0,5,10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100]
_SEC = [0,65.4,118.7,164.0,203.0,238.0,271.0,302.0,331.0,359.5,387.0,
        414.0,440.5,467.0,493.0,518.0,544.0,568.5,593.0,617.0,640.0]

def p_travel_s(dist_km):
    deg = min(max(dist_km / 111.195, 0.0), 100.0)
    return float(np.interp(deg, _DEG, _SEC))

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def fetch_usgs(days=365, minmag=MIN_MAG, limit=1000):
    end_ts = time.time()
    start_ts = end_ts - days * 86400
    url = (
        f'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
        f'&minmagnitude={minmag}&orderby=time&limit={limit}'
        f'&starttime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start_ts))}'
        f'&endtime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end_ts))}'
    )
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    return [{'mag': f['properties']['mag'],
             'lat': f['geometry']['coordinates'][1],
             'lon': f['geometry']['coordinates'][0],
             'time': f['properties']['time'] / 1000,
             'place': f['properties'].get('place', '?')}
            for f in data['features'] if f['properties']['mag']]

def normalize(w):
    w = w.copy()
    for i in range(3):
        w[i] /= (w[i].std() + 1e-6)
    return w

def save(window, label, station, unix_ts, conf, out_dir):
    fname = f'{unix_ts:.3f}_{station.replace(".", "_")}_{label}.npz'
    fpath = os.path.join(out_dir, fname)
    if os.path.exists(fpath):
        return False
    np.savez(fpath, window=window.astype(np.float32),
             label=label, station=station, unix_ts=unix_ts, conf=conf)
    return True

def get_waveform_window(client, net, sta, p_est):
    """Fetch, filter, resample and extract WIN_SAMPLES around p_est - P_LEAD_S."""
    t0 = UTCDateTime(p_est - P_LEAD_S - 3.0)
    t1 = UTCDateTime(p_est + WIN_S + 3.0)
    for loc, ch_code in [('*', 'HH?'), ('*', 'BH?'), ('00', 'HH?'), ('10', 'HH?')]:
        try:
            st = client.get_waveforms(net, sta, loc, ch_code, t0, t1)
            if len(st) >= 1:
                break
        except Exception:
            continue
    else:
        return None

    st.merge(fill_value='interpolate')
    st.detrend('demean')
    st.taper(max_percentage=0.05)
    st.filter('bandpass', freqmin=1.0, freqmax=45.0, corners=4)
    st.resample(TARGET_SRATE)

    window = np.zeros((3, WIN_SAMPLES), dtype=np.float32)
    suffixes = ['Z', 'N', 'E']
    fallbacks = ['Z', '1', '2']
    for i in range(3):
        candidates = ([tr for tr in st if tr.stats.channel.endswith(suffixes[i])] or
                      [tr for tr in st if tr.stats.channel.endswith(fallbacks[i])])
        if not candidates:
            return None
        tr = candidates[0]
        p_sample = int((p_est - P_LEAD_S - tr.stats.starttime.timestamp) * TARGET_SRATE)
        p_sample = max(0, p_sample)
        if p_sample + WIN_SAMPLES > len(tr.data):
            return None
        window[i] = tr.data[p_sample:p_sample + WIN_SAMPLES].astype(np.float32)

    return normalize(window)

def collect_noise_for_station(client, net, sta, n, out_dir):
    saved = 0
    offsets = range(6 * 3600, 6 * 3600 + n * 120, 120)  # every 2 min, starting 6h ago
    for off in offsets:
        ts = time.time() - 4 * 86400 - off
        t0, t1 = UTCDateTime(ts), UTCDateTime(ts + WIN_S + 3.0)
        for loc, ch in [('*', 'HH?'), ('*', 'BH?')]:
            try:
                st = client.get_waveforms(net, sta, loc, ch, t0, t1)
                if len(st) < 1: continue
                st.merge(fill_value='interpolate')
                st.detrend('demean')
                st.filter('bandpass', freqmin=1.0, freqmax=45.0, corners=4)
                st.resample(TARGET_SRATE)
                window = np.zeros((3, WIN_SAMPLES), dtype=np.float32)
                ok = True
                for i, s in enumerate(['Z', 'N', 'E']):
                    trs = [tr for tr in st if tr.stats.channel.endswith(s)]
                    if not trs: ok = False; break
                    if len(trs[0].data) < WIN_SAMPLES: ok = False; break
                    window[i] = trs[0].data[:WIN_SAMPLES].astype(np.float32)
                if ok:
                    window = normalize(window)
                    if save(window, 'noise', f'{net}.{sta}', ts, 0.0, out_dir):
                        saved += 1
                break
            except Exception:
                continue
        if saved >= n:
            break
    return saved

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--out',      default='./training',  help='Output directory')
    ap.add_argument('--days',     type=int, default=365, help='Days of USGS history')
    ap.add_argument('--max-per-station', type=int, default=200)
    ap.add_argument('--noise-per-station', type=int, default=50)
    ap.add_argument('--dry-run',  action='store_true')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    print(f'Fetching USGS events (M≥{MIN_MAG}, {args.days} days)...')
    events = fetch_usgs(days=args.days, minmag=MIN_MAG)
    print(f'  {len(events)} events found')

    client = Client('IRIS', timeout=30)
    total_pos, total_noise = 0, 0

    for sta_key, (sta_lat, sta_lon) in STATIONS.items():
        net, sta = sta_key.split('.')
        print(f'\n── {sta_key} ({sta_lat:.1f}°, {sta_lon:.1f}°) ──')

        # filter events to target distance range
        candidates = []
        for ev in events:
            dist_km = haversine_km(sta_lat, sta_lon, ev['lat'], ev['lon'])
            dist_deg = dist_km / 111.195
            if TARGET_DIST_DEG[0] <= dist_deg <= TARGET_DIST_DEG[1]:
                candidates.append((dist_deg, ev))
        candidates.sort(key=lambda x: (-x[1]['mag'], x[0]))  # prefer larger events
        print(f'  {len(candidates)} events in {TARGET_DIST_DEG[0]}–{TARGET_DIST_DEG[1]}°')

        if args.dry_run:
            for dist_deg, ev in candidates[:10]:
                print(f'    M{ev["mag"]:.1f} {ev["place"]} Δ={dist_deg:.1f}°')
            continue

        pos_saved = 0
        for dist_deg, ev in candidates:
            if pos_saved >= args.max_per_station:
                break
            p_est = ev['time'] + p_travel_s(haversine_km(sta_lat, sta_lon, ev['lat'], ev['lon']))
            w = get_waveform_window(client, net, sta, p_est)
            if w is None:
                sys.stdout.write('.')
            else:
                ok = save(w, 'positive', sta_key, p_est, 1.0, args.out)
                sys.stdout.write('+' if ok else 's')
                if ok: pos_saved += 1
            sys.stdout.flush()
            time.sleep(0.3)  # be polite to IRIS
        print(f'\n  {pos_saved} positive windows saved')

        # noise
        n_noise = collect_noise_for_station(client, net, sta, args.noise_per_station, args.out)
        print(f'  {n_noise} noise windows saved')
        total_pos += pos_saved
        total_noise += n_noise

    print(f'\n=== Done: {total_pos} positives, {total_noise} noise windows ===')
    existing = len([f for f in os.listdir(args.out) if f.endswith('.npz')])
    print(f'Total training files: {existing}')

if __name__ == '__main__':
    main()
