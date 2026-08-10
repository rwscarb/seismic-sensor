import dataclasses
import json
import os
import threading
import time

from seismic.config import DETECTIONS_PATH, SERVER_START_TIME


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

    def update_station(self, key, conf, mag_est):
        with self._lock:
            self.stations[key] = StationSnap(conf=conf, mag_est=mag_est, last_ts=time.time())

    def add_detection(self, det):
        with self._lock:
            self.detections.append(det)
            if len(self.detections) > 500:
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

    def get_detection(self, ref_unix):
        with self._lock:
            for det in reversed(self.detections):
                if abs(det.unix_ts - ref_unix) < 30:
                    return det
        return None

    def to_dict(self):
        with self._lock:
            return {
                'stations': {k: dataclasses.asdict(v) for k, v in self.stations.items()},
                'detections': [
                    {**dataclasses.asdict(d), 'stations': list(d.stations)}
                    for d in self.detections[-200:]
                ],
                'now': time.time(),
                'server_start': SERVER_START_TIME,
            }


sensor_state = SensorState()
