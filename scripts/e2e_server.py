#!/usr/bin/env python3
"""
Boots just the Flask/web layer for Cypress to test against — no torch,
seedlink, or model checkpoints required. The page-load path doesn't need
live sensor data; endpoints that do (e.g. /api/backfill) degrade gracefully
when _ensemble_models is unset (503), which is fine for these tests.

Usage: python3 scripts/e2e_server.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('STATIONS', 'GE.APE,GE.MORC,GE.WLF')
os.environ.setdefault('WEB_PORT', os.environ.get('E2E_PORT', '8099'))

from seismic.web import start_web_server  # noqa: E402

start_web_server()
print('e2e server ready', flush=True)
while True:
    time.sleep(1)
