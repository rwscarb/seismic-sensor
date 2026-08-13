"""
Mock SeedLink injector for local development (MOCK=1).

Drives on_inference() directly — no real SeedLink connection, no ML models needed.
Simulates:
  - Per-station ambient noise ticks (low conf, random)
  - Periodic seismic events: burst of high-conf calls across MOCK_EVENT_STATIONS
    stations, staggered by realistic P-wave travel time differences

ENV knobs:
  MOCK_EVENT_INTERVAL_S   seconds between synthetic events   (default 60)
  MOCK_EVENT_STATIONS     comma-sep station keys to fire     (default: first N_CONSENSUS stations)
  MOCK_NOISE_INTERVAL_S   seconds between noise ticks/sta    (default 3)
"""
import os
import random
import time
import threading
import math

from seismic.config import (
    ALL_STATIONS, N_CONSENSUS, P_VEL_KM_S, THRESHOLD,
)
from seismic.consensus import on_inference
from seismic.localize import station_coords, KNOWN_COORDS
from seismic.state import sensor_state

MOCK_EVENT_INTERVAL_S  = float(os.environ.get('MOCK_EVENT_INTERVAL_S', '60'))
MOCK_NOISE_INTERVAL_S  = float(os.environ.get('MOCK_NOISE_INTERVAL_S', '3'))
_event_stations_raw    = os.environ.get('MOCK_EVENT_STATIONS', '')

# Synthetic epicenter — somewhere in the Mediterranean for variety
_EPICENTERS = [
    (37.5,  15.1,  4.8),   # Sicily, Italy
    (38.1,  21.7,  4.2),   # W Greece
    (40.7,  29.0,  5.1),   # Izmit, Turkey
    (35.5,  23.8,  4.5),   # Crete
    (36.3,  -2.5,  4.0),   # SE Spain
    (69.8, -18.5,  4.7),   # N Norway (near DK stations)
]


def _event_station_keys():
    if _event_stations_raw:
        return [k.strip() for k in _event_stations_raw.split(',') if k.strip()]
    keys = list(station_coords.keys()) or [f"{n}.{s}" for n, s in ALL_STATIONS]
    return keys[:max(N_CONSENSUS, 2)]


def _great_circle_km(lat1, lon1, lat2, lon2):
    """Approximate great-circle distance in km."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _noise_loop():
    """Continuously emit low-conf noise ticks for every station."""
    all_keys = [f"{n}.{s}" for n, s in ALL_STATIONS]
    while True:
        for key in all_keys:
            net, sta = key.split('.', 1)
            conf = random.gauss(0.08, 0.06)
            conf = max(0.0, min(0.45, conf))
            mag  = random.gauss(0.5, 1.2)
            gap  = random.gauss(-0.8, 0.5)
            on_inference(net, sta, conf, mag, gap, time.time())
            time.sleep(MOCK_NOISE_INTERVAL_S / max(1, len(all_keys)))
        time.sleep(0.1)


def _event_loop():
    """Periodically inject a synthetic seismic event."""
    # Initial delay so the UI has time to load before first event
    time.sleep(max(10.0, MOCK_EVENT_INTERVAL_S * 0.2))
    while True:
        epi_lat, epi_lon, true_mag = random.choice(_EPICENTERS)
        keys = _event_station_keys()
        print(f"\n[mock] Synthetic event: M{true_mag:.1f} near "
              f"{epi_lat:.1f}N {epi_lon:.1f}E → firing {keys}", flush=True)

        # Stagger arrivals by P-wave travel time from epicenter to each station
        arrivals = []
        for key in keys:
            coord = station_coords.get(key) or KNOWN_COORDS.get(key)
            if coord:
                dist_km = _great_circle_km(epi_lat, epi_lon, coord[0], coord[1])
            else:
                dist_km = random.uniform(200, 2000)
            delay_s = dist_km / P_VEL_KM_S
            arrivals.append((key, delay_s))

        # Sort by arrival time; fire each after its delay
        t0 = time.time()
        arrivals.sort(key=lambda x: x[1])
        base_delay = arrivals[0][1]  # normalise so first station fires ~now

        for key, delay_s in arrivals:
            fire_at = t0 + (delay_s - base_delay)
            wait = fire_at - time.time()
            if wait > 0:
                time.sleep(wait)
            net, sta = key.split('.', 1)
            conf     = random.gauss(0.93, 0.03)
            conf     = max(THRESHOLD + 0.05, min(0.999, conf))
            mag      = random.gauss(true_mag, 0.3)
            gap      = random.gauss(1.8, 0.4)   # strong gap = model certain
            on_inference(net, sta, conf, mag, gap, time.time())

        # A few residual ticks at slightly lower conf to simulate coda
        time.sleep(2.0)
        for key in keys:
            net, sta = key.split('.', 1)
            conf = random.gauss(0.72, 0.08)
            conf = max(0.0, min(0.95, conf))
            on_inference(net, sta, conf, random.gauss(true_mag, 0.5),
                         random.gauss(0.9, 0.3), time.time())

        time.sleep(MOCK_EVENT_INTERVAL_S)


def run_mock():
    """Entry point — starts noise + event threads, then blocks."""
    # Seed station coords from KNOWN_COORDS so localization works offline
    for key, coord in KNOWN_COORDS.items():
        if key not in station_coords:
            station_coords[key] = coord

    # Seed station state so the UI shows something immediately
    for n, s in ALL_STATIONS:
        key = f"{n}.{s}"
        sensor_state.update_station(key, 0.05, 0.0)

    print("\n[mock] Mock injector active.", flush=True)
    print(f"[mock]   noise tick interval : {MOCK_NOISE_INTERVAL_S}s/station", flush=True)
    print(f"[mock]   event interval      : {MOCK_EVENT_INTERVAL_S}s", flush=True)
    print(f"[mock]   event stations      : {_event_station_keys()}", flush=True)
    print("[mock]   Set MOCK_EVENT_INTERVAL_S / MOCK_EVENT_STATIONS to tune.\n", flush=True)

    threading.Thread(target=_noise_loop, daemon=True, name='mock-noise').start()
    threading.Thread(target=_event_loop, daemon=True, name='mock-events').start()

    # Block main thread
    while True:
        time.sleep(60)
