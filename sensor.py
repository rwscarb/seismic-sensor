#!/usr/bin/env python3
"""
Live Seismic Detection Sensor — multi-station consensus + TDOA epicenter localization.

Loads pre-trained StreamingNet ensemble from /checkpoints/,
connects to a SeedLink server, runs inference on each station independently,
fires alert when N_CONSENSUS stations agree within CONSENSUS_WINDOW seconds,
and estimates the epicenter via TDOA least-squares when 3+ stations have arrivals.

All config via environment variables (see .env / fly.toml).

STATIONS format: "GE.APE,GE.MORC,GE.BORG,GE.KBS"  (NET.STA pairs, comma-separated)
"""
import os
import threading
import time

import sys

from seismic.config import (
    SEEDLINK_SERVER, IRIS_SERVER, STATIONS, IRIS_STATIONS, ALL_STATIONS,
    TUI_MODE, CHANNELS, THRESHOLD, N_CONSENSUS, CONSENSUS_WINDOW,
    ALERT_COOLDOWN, P_VEL_KM_S, LOC_MIN_STA,
    CHECKPOINT_DIR, N_SEEDS, DEVICE, P_LEAD_S,
)

MOCK = os.environ.get('MOCK', '').lower() in ('1', 'true', 'yes')
from seismic.consensus import init_station_state
from seismic.localize import fetch_station_coords, station_coords, KNOWN_COORDS
from seismic.model import load_ensemble, load_phasenet
from seismic.seedlink import seedlink_loop
from seismic.state import sensor_state, _load_detections
from seismic.tui import run_tui
from seismic.watcher import poll_usgs_significant
from seismic.web import start_web_server
from seismic.btcvm_anchor import start_batch_scheduler


def run_sensor(models):
    # Web server + state already initialized before model loading (see __main__)

    startup_delay = int(os.environ.get('STARTUP_DELAY', '8'))
    if startup_delay > 0:
        print(f"Waiting {startup_delay}s for network...", flush=True)
        time.sleep(startup_delay)

    print("\nFetching station coordinates...", flush=True)
    try:
        fetch_station_coords()
    except Exception as e:
        print(f"  coords fetch failed ({e}) — using hardcoded fallback", flush=True)
        for net, sta in ALL_STATIONS:
            key = f"{net}.{sta}"
            if key in KNOWN_COORDS:
                station_coords[key] = KNOWN_COORDS[key]

    station_list = ', '.join(f"{n}.{s}" for n, s in ALL_STATIONS)
    print(f"  Stations:  {station_list}", flush=True)
    print(f"  Channels:  {CHANNELS}", flush=True)
    print(f"  Threshold: {THRESHOLD}  |  Consensus: {N_CONSENSUS}/{len(ALL_STATIONS)} in {CONSENSUS_WINDOW:.0f}s",
          flush=True)
    print(f"  Cooldown:  {ALERT_COOLDOWN}s  |  P-vel: {P_VEL_KM_S} km/s", flush=True)
    print(f"  Localize:  {LOC_MIN_STA}+ stations required", flush=True)

    # USGS significant-event watcher (reverse correlation: catalog → detection log)
    threading.Thread(
        target=poll_usgs_significant,
        daemon=True,
        name='usgs-sig-watch',
    ).start()

    # Build per-server station groups; always run GEOFON, optionally IRIS
    server_groups = [(SEEDLINK_SERVER, STATIONS)]
    if IRIS_STATIONS:
        server_groups.append((IRIS_SERVER, IRIS_STATIONS))

    if TUI_MODE:
        print("TUI mode — starting Rich display...", flush=True)
        for srv, stas in server_groups:
            t = threading.Thread(target=seedlink_loop, args=(srv, stas, models),
                                 daemon=True, name=f'seedlink-{srv.split(":")[0]}')
            t.start()
        run_tui()
    elif len(server_groups) == 1:
        print("Ready. Ctrl+C to stop.\n", flush=True)
        seedlink_loop(server_groups[0][0], server_groups[0][1], models)
    else:
        print("Ready. Ctrl+C to stop.\n", flush=True)
        for srv, stas in server_groups[1:]:
            t = threading.Thread(target=seedlink_loop, args=(srv, stas, models),
                                 daemon=True, name=f'seedlink-{srv.split(":")[0]}')
            t.start()
        seedlink_loop(server_groups[0][0], server_groups[0][1], models)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Phase 1: restore detections from disk and start Flask immediately so
    # /health responds during the deploy gap while models are still loading.
    init_station_state()
    sensor_state.detections = _load_detections()
    start_web_server()
    start_batch_scheduler()

    if MOCK:
        # Mock mode — no models, no SeedLink; synthetic events drive the real pipeline
        print("Seismic Detection Sensor [MOCK MODE — no SeedLink, no ML models]", flush=True)
        from seismic.mock import run_mock
        run_mock()
    else:
        # Phase 2: load ML models (slow — 15-30s; /health already answering above)
        print("Seismic Detection Sensor (multi-station consensus + TDOA localization)", flush=True)
        print(f"  model:   StreamingNet {N_SEEDS}-seed ensemble (H-{P_LEAD_S}s, mean-conf)", flush=True)
        print(f"  device:  {DEVICE}", flush=True)
        print(f"\nLoading checkpoints from {CHECKPOINT_DIR}...", flush=True)
        models = load_ensemble()
        print(f"  {N_SEEDS} models loaded.\n", flush=True)
        print("Loading PhaseNet (SeisBench)...", flush=True)
        load_phasenet()

        # Phase 3: connect to SeedLink and stream
        run_sensor(models)
