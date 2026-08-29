import collections
import dataclasses
import json
import os
import threading
import time

from seismic.config import DETECTIONS_PATH, SERVER_START_TIME, CONF_HISTORY_DEPTH

FLATLINE_VARIANCE_THRESH = 1e-5  # conf history variance below this = stuck/flatline
FLATLINE_MIN_SAMPLES = 20        # need at least this many samples before declaring flatline

MAX_DETECTIONS = 2000  # in-memory ring size; all are returned to UI


@dataclasses.dataclass
class StationSnap:
    conf: float = 0.0
    mag_est: float = 0.0
    last_ts: float = 0.0


@dataclasses.dataclass
class DetectionSnap:
    ts: str = ''
    unix_ts: float = 0.0
    stations: dataclasses.field(default_factory=list) = None
    conf: float = 0.0
    logit_gap: float = 0.0   # raw mean logit gap before temperature scaling
    mb: float = None
    mb_approx: bool = False   # True when epicenter unknown; Q(Δ) assumed at 45°
    mb_local: bool = False    # True when amplitude ratio suggests a local/regional source
    epicenter: tuple = None   # (lat, lon) or None
    teleseismic: bool = False  # True when locator RMS > threshold → distant source, pin unreliable
    usgs: dict = None         # USGS ComCat event if matched
    usgs_checked: bool = False  # True once USGS lookup has completed (match or not)
    arrival_offsets: dict = None  # station_key → seconds after earliest P-arrival (0.0 = first)

    def __post_init__(self):
        if self.stations is None:
            self.stations = []


def _save_detections(detections):
    try:
        tmp = DETECTIONS_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump([dataclasses.asdict(d) for d in detections], f)
        os.replace(tmp, DETECTIONS_PATH)
    except Exception as e:
        print(f"[persist] save failed: {e}", flush=True)


def _load_detections():
    try:
        with open(DETECTIONS_PATH) as f:
            rows = json.load(f)
        dets = []
        for r in rows:
            d = DetectionSnap(
                ts=r.get('ts', ''),
                unix_ts=r.get('unix_ts', 0.0),
                stations=r.get('stations', []),
                conf=r.get('conf', 0.0),
                logit_gap=r.get('logit_gap', 0.0),
                mb=r.get('mb'),
                mb_approx=r.get('mb_approx', False),
                mb_local=r.get('mb_local', False),
                epicenter=tuple(r['epicenter']) if r.get('epicenter') else None,
                teleseismic=r.get('teleseismic', False),
                usgs=r.get('usgs'),
                usgs_checked=r.get('usgs_checked', False),
                arrival_offsets=r.get('arrival_offsets'),
            )
            dets.append(d)
        print(f"[persist] loaded {len(dets)} detections from {DETECTIONS_PATH}", flush=True)
        return dets
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[persist] load failed: {e} — starting fresh", flush=True)
        return []


class SensorState:
    def __init__(self):
        self._lock = threading.Lock()
        self.stations: dict = {}    # key → StationSnap
        self.detections: list = []  # DetectionSnap, oldest first
        self._conf_history: dict = {}  # key → deque of recent conf floats

    def update_station(self, key, conf, mag_est):
        with self._lock:
            self.stations[key] = StationSnap(conf=conf, mag_est=mag_est, last_ts=time.time())
            # Append to per-station conf history ringbuffer
            if key not in self._conf_history:
                self._conf_history[key] = collections.deque(maxlen=CONF_HISTORY_DEPTH)
            self._conf_history[key].append(round(conf, 4))

    def add_detection(self, det):
        with self._lock:
            self.detections.append(det)
            if len(self.detections) > MAX_DETECTIONS:
                self.detections.pop(0)
            snap = list(self.detections)
        _save_detections(snap)

    def update_mb(self, ref_unix, mb, approx=False, local=False):
        with self._lock:
            for det in reversed(self.detections):
                if abs(det.unix_ts - ref_unix) < 30:
                    det.mb = mb
                    det.mb_approx = approx
                    det.mb_local = local
                    break
            snap = list(self.detections)
        _save_detections(snap)

    def update_usgs(self, ref_unix, event):
        with self._lock:
            for det in reversed(self.detections):
                if abs(det.unix_ts - ref_unix) < 30:
                    det.usgs = event
                    det.usgs_checked = True
                    break
            snap = list(self.detections)
        _save_detections(snap)

    def is_flatline(self, key):
        """Public lock-safe flatline check for use outside to_dict."""
        with self._lock:
            return self._is_flatline(key)

    def _is_flatline(self, key, hist=None):
        """Internal flatline check — call with lock held or pass hist explicitly."""
        if hist is None:
            hist = list(self._conf_history.get(key, []))
        if len(hist) < FLATLINE_MIN_SAMPLES:
            return False
        import statistics
        try:
            return statistics.variance(hist) < FLATLINE_VARIANCE_THRESH
        except Exception:
            return False

    def get_detection(self, ref_unix):
        with self._lock:
            for det in reversed(self.detections):
                if abs(det.unix_ts - ref_unix) < 30:
                    return det
        return None

    def to_dict(self, include_detections=True):
        """include_detections=False skips serializing the (growing, currently
        up to MAX_DETECTIONS) detection list — the expensive part of this call
        under lock. Used by the frequent live-station poll, which only needs
        station liveness; detections_count/latest_detection_ts are O(1) and
        let callers cheaply notice when a real detections re-fetch is due."""
        with self._lock:
            stations_out = {}
            for k, v in self.stations.items():
                d = dataclasses.asdict(v)
                hist = list(self._conf_history.get(k, []))
                d['conf_history'] = hist
                d['flatline'] = self._is_flatline(k, hist)
                stations_out[k] = d
            out = {
                'stations': stations_out,
                'now': time.time(),
                'server_start': SERVER_START_TIME,
                'detections_count': len(self.detections),
                'latest_detection_ts': self.detections[-1].unix_ts if self.detections else None,
            }
            if include_detections:
                out['detections'] = [
                    {**dataclasses.asdict(d), 'stations': list(d.stations)}
                    for d in self.detections
                ]
            return out


sensor_state = SensorState()
