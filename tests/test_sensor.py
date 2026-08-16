"""
Unit tests for seismic-sensor — no torch/obspy required.
Imports from the seismic/ package submodules; stubs heavy deps before import.
"""
import sys
import math
import types
import threading
import dataclasses
import time
import unittest
from unittest.mock import MagicMock

# ── Stub out torch, obspy, scipy, flask, rich before sensor.py imports them ───
def _make_torch_stub():
    torch = types.ModuleType('torch')
    torch.nn = types.ModuleType('torch.nn')
    torch.nn.Module = object
    torch.nn.Sequential = MagicMock()
    torch.nn.Conv1d = MagicMock()
    torch.nn.BatchNorm1d = MagicMock()
    torch.nn.ReLU = MagicMock()
    torch.nn.Linear = MagicMock()
    torch.nn.AdaptiveAvgPool1d = MagicMock()
    torch.nn.functional = types.ModuleType('torch.nn.functional')
    torch.tensor = MagicMock(return_value=MagicMock())
    torch.zeros_like = MagicMock(return_value=MagicMock())
    torch.load = MagicMock(return_value={})
    torch.no_grad = MagicMock(return_value=MagicMock(__enter__=lambda s, *a: None, __exit__=lambda s, *a: None))
    torch.relu = MagicMock()
    torch.long = 'long'
    return torch

def _make_flask_stub():
    flask = types.ModuleType('flask')
    flask.Flask = MagicMock()
    flask.jsonify = MagicMock()
    flask.Response = MagicMock()
    return flask

def _make_rich_stub():
    rich = types.ModuleType('rich')
    rich.live = types.ModuleType('rich.live')
    rich.live.Live = MagicMock()
    rich.table = types.ModuleType('rich.table')
    rich.table.Table = MagicMock()
    rich.console = types.ModuleType('rich.console')
    rich.console.Console = MagicMock()
    rich.text = types.ModuleType('rich.text')
    rich.text.Text = MagicMock()
    return rich

for mod_name, factory in [
    ('torch', _make_torch_stub),
    ('torch.nn', lambda: _make_torch_stub().nn),
    ('torch.nn.functional', lambda: _make_torch_stub().nn.functional),
    ('flask', _make_flask_stub),
    ('rich', _make_rich_stub),
    ('rich.live', lambda: _make_rich_stub().live),
    ('rich.table', lambda: _make_rich_stub().table),
    ('rich.console', lambda: _make_rich_stub().console),
    ('rich.text', lambda: _make_rich_stub().text),
]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = factory()

# obspy is optional in tests too
obspy_stub = types.ModuleType('obspy')
obspy_stub.clients = types.ModuleType('obspy.clients')
obspy_stub.clients.fdsn = types.ModuleType('obspy.clients.fdsn')
obspy_stub.clients.fdsn.Client = MagicMock()
sys.modules.setdefault('obspy', obspy_stub)
sys.modules.setdefault('obspy.clients', obspy_stub.clients)
sys.modules.setdefault('obspy.clients.fdsn', obspy_stub.clients.fdsn)

# Now we can import the functions we actually want to test by exec-ing sensor.py
# with a restricted import graph. Instead, just import the functions directly
# by importing the module. Set CHECKPOINT_DIR to /tmp so load_ensemble isn't called at import.
import os
os.environ.setdefault('CHECKPOINT_DIR', '/tmp')

# Add seismic-sensor/ to path so we can import sensor
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from seismic.localize import haversine_km, p_travel_time_s, locate_epicenter, station_coords
from seismic.config import fmt_mag
from seismic.state import SensorState, DetectionSnap, MAX_DETECTIONS

# Build a 'sensor' namespace so test bodies need no changes
import types as _types
sensor = _types.SimpleNamespace(
    haversine_km=haversine_km,
    p_travel_time_s=p_travel_time_s,
    locate_epicenter=locate_epicenter,
    station_coords=station_coords,
    fmt_mag=fmt_mag,
    SensorState=SensorState,
    DetectionSnap=DetectionSnap,
)


class TestHaversine(unittest.TestCase):
    def test_same_point(self):
        self.assertAlmostEqual(sensor.haversine_km(0, 0, 0, 0), 0.0, places=5)

    def test_known_distance(self):
        # London (51.5, -0.1) to Paris (48.85, 2.35) ≈ 344 km
        d = sensor.haversine_km(51.5, -0.1, 48.85, 2.35)
        self.assertGreater(d, 330)
        self.assertLess(d, 360)

    def test_equator_quarter_circle(self):
        # 90° of longitude along equator = π/2 * R ≈ 10007 km
        d = sensor.haversine_km(0, 0, 0, 90)
        self.assertAlmostEqual(d, 10007, delta=5)

    def test_symmetry(self):
        d1 = sensor.haversine_km(48.0, 16.0, 49.7, 6.15)
        d2 = sensor.haversine_km(49.7, 6.15, 48.0, 16.0)
        self.assertAlmostEqual(d1, d2, places=6)


class TestFmtMag(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(sensor.fmt_mag(5.2), 'M5.2')

    def test_zero(self):
        self.assertEqual(sensor.fmt_mag(0.0), 'M0.0')

    def test_negative_clamped(self):
        self.assertEqual(sensor.fmt_mag(-3.0), 'M-2.0')

    def test_saturated(self):
        self.assertEqual(sensor.fmt_mag(8.0), '---')

    def test_boundary(self):
        # exactly at MAG_MAX_CREDIBLE (7.5) is NOT suppressed
        self.assertEqual(sensor.fmt_mag(7.5), 'M7.5')

    def test_just_above_boundary(self):
        self.assertEqual(sensor.fmt_mag(7.51), '---')


class TestSensorState(unittest.TestCase):
    def setUp(self):
        self.state = sensor.SensorState()

    def test_update_station(self):
        self.state.update_station('GE.APE', 0.95, 4.2)
        snap = self.state.stations['GE.APE']
        self.assertAlmostEqual(snap.conf, 0.95)
        self.assertAlmostEqual(snap.mag_est, 4.2)

    def test_add_detection(self):
        det = sensor.DetectionSnap(ts='2026-01-01T00:00:00', unix_ts=1000.0,
                                   stations=['GE.APE', 'GE.MORC'], conf=0.91)
        self.state.add_detection(det)
        self.assertEqual(len(self.state.detections), 1)
        self.assertEqual(self.state.detections[0].conf, 0.91)

    def test_detection_capped_at_max(self):
        n = MAX_DETECTIONS + 5
        for i in range(n):
            self.state.add_detection(sensor.DetectionSnap(unix_ts=float(i), conf=0.9))
        self.assertEqual(len(self.state.detections), MAX_DETECTIONS)

    def test_update_mb(self):
        det = sensor.DetectionSnap(unix_ts=5000.0, conf=0.9)
        self.state.add_detection(det)
        self.state.update_mb(5000.0, 5.3)
        self.assertAlmostEqual(self.state.detections[0].mb, 5.3)

    def test_update_usgs(self):
        det = sensor.DetectionSnap(unix_ts=5000.0, conf=0.9)
        self.state.add_detection(det)
        event = {'mag': 5.3, 'magType': 'mb', 'place': 'Near Alaska', 'lat': 55.0, 'lon': -160.0}
        self.state.update_usgs(5000.0, event)
        self.assertEqual(self.state.detections[0].usgs['mag'], 5.3)

    def test_update_mb_no_match(self):
        det = sensor.DetectionSnap(unix_ts=5000.0, conf=0.9)
        self.state.add_detection(det)
        self.state.update_mb(9999.0, 5.3)  # no match by timestamp
        self.assertIsNone(self.state.detections[0].mb)

    def test_to_dict_structure(self):
        self.state.update_station('GE.APE', 0.88, 3.1)
        d = self.state.to_dict()
        self.assertIn('stations', d)
        self.assertIn('detections', d)
        self.assertIn('now', d)
        self.assertIn('GE.APE', d['stations'])

    def test_thread_safety(self):
        errors = []
        def writer():
            for i in range(50):
                try:
                    self.state.update_station('GE.MORC', float(i) / 100, float(i))
                except Exception as e:
                    errors.append(e)

        def reader():
            for _ in range(50):
                try:
                    self.state.to_dict()
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestLocateEpicenter(unittest.TestCase):
    def setUp(self):
        # Patch sensor.station_coords with known test stations
        self._orig_coords = dict(sensor.station_coords)
        sensor.station_coords.update({
            'GE.APE':  (37.069,  25.531),
            'GE.MORC': (49.781,  16.978),
            'GE.WLF':  (49.664,   6.153),
        })

    def tearDown(self):
        sensor.station_coords.clear()
        sensor.station_coords.update(self._orig_coords)

    def test_returns_none_with_too_few_stations(self):
        arrivals = [('GE.APE', 1000.0), ('GE.MORC', 1002.0)]  # only 2, LOC_MIN_STA=3
        result = sensor.locate_epicenter(arrivals)
        self.assertIsNone(result)

    def test_synthetic_epicenter(self):
        # Generate synthetic arrivals using the same IASP91 travel-time model
        # that locate_epicenter uses, so the inversion is self-consistent.
        epi_lat, epi_lon, t0 = 45.0, 15.0, 0.0
        arrivals = []
        for key in ['GE.APE', 'GE.MORC', 'GE.WLF']:
            lat, lon = sensor.station_coords[key]
            dist = sensor.haversine_km(epi_lat, epi_lon, lat, lon)
            t_arr = t0 + sensor.p_travel_time_s(dist)
            arrivals.append((key, t_arr))

        result = sensor.locate_epicenter(arrivals)
        self.assertIsNotNone(result)
        lat_e, lon_e, t0_e, rms = result
        self.assertAlmostEqual(lat_e, epi_lat, delta=1.0)
        self.assertAlmostEqual(lon_e, epi_lon, delta=1.0)
        self.assertLess(rms, 10.0)  # residual < 10s for noise-free synthetic

    def test_unknown_station_ignored(self):
        # Third station has no coords → should still fail (< LOC_MIN_STA known)
        arrivals = [('GE.APE', 1000.0), ('GE.MORC', 1002.0), ('UNKNOWN.STA', 1003.0)]
        result = sensor.locate_epicenter(arrivals)
        self.assertIsNone(result)


class TestSeedlinkReconnect(unittest.TestCase):
    """Verify that seedlink_loop sets conn.resume=False on each new client.
    This prevents ObsPy from attempting FETCH/TIME on reconnect, which GEOFON rejects.
    """

    def test_resume_false_on_connect(self):
        """conn.resume must be False before client.run() is called."""
        from unittest.mock import patch
        import seismic.seedlink as sl_mod

        captured = {}

        class FakeConn:
            def __init__(self):
                self.resume = True  # ObsPy default is True
                self.streams = []
            def add_stream(self, *a, **kw):
                pass

        class FakeClient:
            def __init__(self, server_url):
                self.conn = FakeConn()
            def select_stream(self, net, sta, ch):
                pass
            def run(self):
                captured['resume'] = self.conn.resume
                raise KeyboardInterrupt  # stop the retry loop cleanly

        # Patch the import inside seedlink_loop — it does a local import of
        # EasySeedLinkClient, so we stub the containing module in sys.modules.
        fake_esl_mod = types.ModuleType('obspy.clients.seedlink.easyseedlink')
        fake_esl_mod.EasySeedLinkClient = FakeClient
        with patch.dict(sys.modules, {
            'obspy.clients.seedlink': types.ModuleType('obspy.clients.seedlink'),
            'obspy.clients.seedlink.easyseedlink': fake_esl_mod,
        }):
            try:
                sl_mod.seedlink_loop('fake:18000', [('GE', 'APE')], [])
            except KeyboardInterrupt:
                pass

        self.assertIn('resume', captured, 'run() was never called')
        self.assertFalse(captured['resume'],
                         'conn.resume must be False to prevent FETCH/TIME on reconnect')


if __name__ == '__main__':
    unittest.main()
