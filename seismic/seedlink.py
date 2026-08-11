import time

import numpy as np

from seismic.config import CHANNELS, TARGET_SRATE, WIN_SAMPLES, STRIDE
from seismic.consensus import station_key, station_rings, station_strides, on_inference
from seismic.model import ensemble_predict, normalize_window


def seedlink_loop(server, stations, models):
    """Connect to a SeedLink server, stream data for given stations, run inference. Retries forever."""
    from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

    class Sensor(EasySeedLinkClient):
        def on_data(self, trace):
            net = trace.stats.network
            sta = trace.stats.station
            ch = trace.stats.channel
            key = station_key(net, sta)

            if key not in station_rings or ch not in CHANNELS:
                return

            data = trace.data.astype(np.float32)
            sr = trace.stats.sampling_rate
            if abs(sr - TARGET_SRATE) > 1.0:
                from obspy import Trace as OTrace
                t = OTrace(data=trace.data.copy(), header=trace.stats)
                t.resample(TARGET_SRATE)
                data = t.data.astype(np.float32)

            ring = station_rings[key]
            ring[ch].extend(data)
            station_strides[key] += len(data)

            if min(len(ring[c]) for c in CHANNELS) < WIN_SAMPLES:
                return
            if station_strides[key] < STRIDE:
                return
            station_strides[key] = 0

            window = np.array([
                list(ring[CHANNELS[0]])[-WIN_SAMPLES:],
                list(ring[CHANNELS[1]])[-WIN_SAMPLES:],
                list(ring[CHANNELS[2]])[-WIN_SAMPLES:],
            ], dtype=np.float32)

            conf, mag_est, logit_gap = ensemble_predict(models, normalize_window(window))
            on_inference(net, sta, conf, mag_est, logit_gap, time.time())

    print(f"\nConnecting to {server} ({len(stations)} station(s))...", flush=True)
    backoff = 5
    while True:
        try:
            client = Sensor(server)
            for net, sta in stations:
                for ch in CHANNELS:
                    client.select_stream(net, sta, ch)
            backoff = 5
            client.run()
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
            break
        except BaseException as e:
            print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] [{server}] "
                  f"Connection error ({type(e).__name__}): {e}", flush=True)
            print(f"  Retrying in {backoff}s...", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
