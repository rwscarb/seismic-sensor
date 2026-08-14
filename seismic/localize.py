import math

import numpy as np

from seismic.config import STATIONS, IRIS_STATIONS, ALL_STATIONS, LOC_MIN_STA

# ── Known station coordinates (lat, lon) — fallback if FDSN fetch fails ───────
KNOWN_COORDS = {
    'GE.APE':  (37.069,  25.531),   # Aegean, Greece (FDSN-confirmed)
    'GE.MORC': (49.781,  16.978),   # Morava, Czech Republic
    'GE.BORG': (64.747, -21.328),   # Borgarfjordur, Iceland
    'GE.KBS':  (78.926,  11.943),   # Ny-Ålesund, Svalbard
    'GE.WLF':  (49.664,   6.153),   # Walferdange, Luxembourg
    'GE.STU':  (48.771,   9.194),   # Stuttgart, Germany
    'GE.MAHO': (39.932,   4.267),   # Mahon, Menorca, Spain
    'GE.MTE':  (38.528,  -7.538),   # Mértola, Portugal
    'GE.MATE': (40.649,  16.704),   # Matera, Italy
    'GE.KARP': (35.784,  27.154),   # Karpathos, Greece
    # South America — Chilean Seismological Network (IPOC), via GEOFON
    'CX.PSGCX': (-34.290,  -72.033),  # Pichilemu, Chile
    'CX.HMBCX': (-36.926,  -73.030),  # Hualqui, Chile
    # North Atlantic / Arctic — Danish Seismological Network, via GEOFON
    'DK.GDH':   ( 69.249,  -53.528),  # Godthab (Nuuk), W Greenland
    'DK.SCO':   ( 70.483,  -21.967),  # Scoresbysund, E Greenland
}

station_coords = {}    # populated at startup
station_inventory = {}  # key → obspy Inventory with instrument response


def _fetch_coords_from(fdsn_client_name, station_list):
    """Try to fetch FDSN coords + response for a list of (net, sta) pairs."""
    from obspy.clients.fdsn import Client
    try:
        client = Client(fdsn_client_name)
    except Exception as e:
        print(f"  [fdsn] {fdsn_client_name} client init failed: {e}", flush=True)
        client = None
    for net, sta in station_list:
        key = f"{net}.{sta}"
        fetched = False
        if client:
            try:
                inv = client.get_stations(network=net, station=sta, level="response")
                st = inv[0][0]
                station_coords[key] = (st.latitude, st.longitude)
                station_inventory[key] = inv
                print(
                    f"  coords {key}: {st.latitude:.3f}°N {st.longitude:.3f}°E (FDSN+response)",
                    flush=True,
                )
                fetched = True
            except Exception:
                try:
                    inv_s = client.get_stations(network=net, station=sta, level="station")
                    st = inv_s[0][0]
                    station_coords[key] = (st.latitude, st.longitude)
                    print(
                        f"  coords {key}: {st.latitude:.3f}°N {st.longitude:.3f}°E (FDSN, no response)",
                        flush=True,
                    )
                    fetched = True
                except Exception:
                    pass
        if not fetched:
            if key in KNOWN_COORDS:
                station_coords[key] = KNOWN_COORDS[key]
                lat, lon = KNOWN_COORDS[key]
                print(f"  coords {key}: {lat:.3f}°N {lon:.3f}°E (hardcoded)", flush=True)
            else:
                print(f"  coords {key}: unknown — will skip in localization", flush=True)


def fetch_station_coords():
    """Fetch FDSN coords + instrument response; fall back to hardcoded gracefully."""
    try:
        if STATIONS:
            print(f"  [geofon] fetching {len(STATIONS)} station(s)...", flush=True)
            _fetch_coords_from("GEOFON", STATIONS)
        if IRIS_STATIONS:
            print(f"  [iris] fetching {len(IRIS_STATIONS)} station(s)...", flush=True)
            _fetch_coords_from("IRIS", IRIS_STATIONS)
    except Exception as e:
        print(f"  [fdsn] fetch error: {e} — using hardcoded fallback", flush=True)
        for net, sta in ALL_STATIONS:
            key = f"{net}.{sta}"
            if key in KNOWN_COORDS:
                station_coords[key] = KNOWN_COORDS[key]


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# IASP91 P travel times (seconds) for shallow focus (h≈15 km), keyed by distance (degrees).
# Source: Kennett & Engdahl (1991), interpolated from TauPy; values at 0° extrapolated to 0.
_IASP91_DEG = np.array(
    [0, 2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 90, 100, 110, 120, 150, 180],
    dtype=np.float64)
_IASP91_SEC = np.array(
    [0, 27.0, 64.7, 130.7, 196.5, 256.8, 321.0, 383.5, 444.5, 507.0,
     566.7, 628.5, 689.2, 747.0, 802.5, 855.0, 905.0, 952.0, 1038.0,
     1116.0, 1185.0, 1250.0, 1390.0, 1510.0],
    dtype=np.float64)


def p_travel_time_s(dist_km):
    """IASP91 P travel time (seconds) via linear interpolation of tabulated values."""
    dist_deg = min(max(dist_km / 111.195, 0.0), 180.0)
    return float(np.interp(dist_deg, _IASP91_DEG, _IASP91_SEC))


# Richter (1958) Q(Δ) table for body-wave magnitude; amplitude in µm.
# We measure in nm, so subtract log10(1e6/1e9)=−3 → effectively subtract 3 from these values.
_RICHTER_Q_DEG = np.array(
    [2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100],
    dtype=np.float64)
_RICHTER_Q_VAL = np.array(
    [3.96, 4.88, 5.26, 5.57, 5.84, 6.07, 6.24, 6.36, 6.44, 6.46,
     6.44, 6.38, 6.33, 6.27, 6.21, 6.14, 6.09, 6.04, 6.01, 5.99, 5.97],
    dtype=np.float64)


def richter_q_nm(dist_deg):
    """Richter (1958) Q(Δ) correction, adjusted for amplitude in nm (µm table − 3)."""
    dist_deg = min(max(dist_deg, 2.0), 100.0)
    return float(np.interp(dist_deg, _RICHTER_Q_DEG, _RICHTER_Q_VAL)) - 3.0


def locate_epicenter(arrivals, sp_distances=None):
    """
    arrivals    : list of (station_key, p_arrival_unix)
    sp_distances: optional {station_key: dist_km} from PhaseNet S-P picks

    Returns (lat, lon, origin_time_unix, rms_s) or None.

    Method:
      1. Global 5° grid search to find the basin of attraction.
      2. Nelder-Mead refinement from the best grid point.
      IASP91 P travel-time table is used instead of a fixed velocity.
      When sp_distances are provided, they add distance-anchoring terms to the
      cost that are independent of origin time — significantly reducing degeneracy.
    """
    from scipy.optimize import minimize

    obs = [(key, t) for key, t in arrivals if key in station_coords]
    if len(obs) < LOC_MIN_STA:
        return None

    sta_lat = np.array([station_coords[k][0] for k, _ in obs])
    sta_lon = np.array([station_coords[k][1] for k, _ in obs])
    arr_time = np.array([t for _, t in obs])

    # Stations + known S-P distances (independent of origin time)
    sp_keys = []
    sp_km = []
    if sp_distances:
        for k, d in sp_distances.items():
            if k in station_coords:
                sp_keys.append(k)
                sp_km.append(d)

    def cost(params):
        lat0, lon0 = params
        dists = np.array([haversine_km(lat0, lon0, sta_lat[i], sta_lon[i])
                          for i in range(len(obs))])
        travel = np.array([p_travel_time_s(d) for d in dists])
        # Use median (not mean) so a single bad pick doesn't pull t0 into a
        # false minimum — the mean estimator absorbs TDOA errors into t0,
        # artificially flattening the cost landscape at wrong candidate locations.
        t0_opt = float(np.median(arr_time - travel))
        pred = t0_opt + travel
        tdoa_res = float(np.sum((pred - arr_time) ** 2))

        # S-P distance constraint: penalise deviation from PhaseNet-derived distance.
        # Weight chosen so one S-P constraint ≈ two TDOA constraints.
        sp_res = 0.0
        if sp_keys:
            for k, target_km in zip(sp_keys, sp_km):
                slat, slon = station_coords[k]
                actual_km = haversine_km(lat0, lon0, slat, slon)
                sp_res += (actual_km - target_km) ** 2 / (target_km * 0.2) ** 2
            sp_res *= (tdoa_res / max(len(obs), 1)) * 2

        return tdoa_res + sp_res

    # ── Stage 1: coarse global grid search at 5° resolution ──────────────────
    # Collect the top-8 candidates to guard against false minima in the flat
    # teleseismic regime where a single-start refinement often lands in the wrong basin.
    grid_candidates = []
    for glat in range(-85, 91, 5):
        for glon in range(-180, 181, 5):
            c = cost((glat, glon))
            grid_candidates.append((c, glat, glon))
    grid_candidates.sort(key=lambda x: x[0])
    top_starts = [(g[1], g[2]) for g in grid_candidates[:8]]

    # ── Stage 2: Nelder-Mead refinement from each top candidate; keep global best ──
    best_res = None
    best_final_cost = float('inf')
    nm_opts = {'xatol': 0.02, 'fatol': 0.05, 'maxiter': 100000}
    for start in top_starts:
        r = minimize(cost, start, method='Nelder-Mead', options=nm_opts)
        if r.fun < best_final_cost:
            best_final_cost = r.fun
            best_res = r
    res = best_res

    lat_e, lon_e = res.x
    dists_final = np.array([
        haversine_km(lat_e, lon_e, sta_lat[i], sta_lon[i])
        for i in range(len(obs))
    ])
    travel_final = np.array([p_travel_time_s(d) for d in dists_final])
    t0_e = float(np.mean(arr_time - travel_final))
    # RMS uses only TDOA residuals for interpretability
    t0_pred = t0_e + travel_final
    rms = math.sqrt(float(np.mean((t0_pred - arr_time) ** 2)))

    lat_e = max(-90.0, min(90.0, lat_e))
    lon_e = ((lon_e + 180) % 360) - 180

    return lat_e, lon_e, t0_e, rms
