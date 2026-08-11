import math
import time

import numpy as np

from seismic.config import TARGET_SRATE, MB_DELAY_S, MB_WIN_S
from seismic.localize import station_coords, station_inventory, haversine_km, richter_q_nm
from seismic.state import sensor_state


def estimate_mb(key, p_arrival_unix, epicenter_latlon):
    """
    mb = log10(A / T) + Q(Δ)
    A   = peak ground displacement (nm) from instrument-corrected HHZ
    T   = dominant period at the peak (s)
    Q   = empirical attenuation correction (Richter 1958, shallow teleseismic P)
    Returns (mb_float, None) or (None, reason_str).
    """
    from seismic.consensus import station_rings  # late import to avoid circular dependency

    if key not in station_inventory:
        return None, "no response inventory"
    if key not in station_rings or 'HHZ' not in station_rings[key]:
        return None, "no HHZ ring"

    ring = station_rings[key]['HHZ']
    data = np.array(list(ring), dtype=np.float32)
    if len(data) < 200:
        return None, "insufficient buffer"

    from obspy import Trace as OTrace, UTCDateTime
    buf_end = time.time()
    buf_start = buf_end - len(data) / TARGET_SRATE

    tr = OTrace(data=data.copy())
    tr.stats.network = key.split('.')[0]
    tr.stats.station = key.split('.')[1]
    tr.stats.channel = 'HHZ'
    tr.stats.sampling_rate = TARGET_SRATE
    tr.stats.starttime = UTCDateTime(buf_start)

    try:
        tr.remove_response(inventory=station_inventory[key],
                           output="DISP", water_level=60,
                           pre_filt=(0.005, 0.01, 3.0, 5.0))
        # IASPEI mb is defined in the 1 Hz band — bandpass before measuring A and T
        tr.filter('bandpass', freqmin=0.5, freqmax=2.0, corners=4, zerophase=True)
    except Exception as e:
        return None, f"response removal: {e}"

    data_nm = tr.data * 1e9  # m → nm

    p_idx = max(0, int((p_arrival_unix - buf_start) * TARGET_SRATE))
    p_win = data_nm[p_idx: p_idx + int(MB_WIN_S * TARGET_SRATE)]
    if len(p_win) < 50:
        return None, "P-window outside buffer"

    A = float(np.abs(p_win).max())
    if A <= 0:
        return None, "zero amplitude"

    # Measure T from zero crossings; after 1 Hz bandpass T should be ~0.5-2s
    zc = np.where(np.diff(np.sign(p_win)))[0]
    T = float(2.0 * np.mean(np.diff(zc)) / TARGET_SRATE) if len(zc) >= 4 else 1.0
    T = max(0.5, min(2.0, T))   # IASPEI: constrain to teleseismic P-wave band

    # Q(Δ) — Richter (1958) table approximation for shallow focus.
    # Note: A above is in nm; GR tables assume µm → subtract log10(1000)=3 from Q constants.
    approx = False
    if epicenter_latlon is not None and key in station_coords:
        sta_lat, sta_lon = station_coords[key]
        epi_lat, epi_lon = epicenter_latlon
        dist_deg = haversine_km(sta_lat, sta_lon, epi_lat, epi_lon) / 111.195
        dist_deg = max(2.0, min(100.0, dist_deg))
    else:
        dist_deg = 45.0   # mid-range teleseismic assumption; ±1 mag unit uncertainty
        approx = True

    Q = richter_q_nm(dist_deg)
    mb = max(0.0, min(10.0, math.log10(A / T) + Q))
    approx_flag = '~' if approx else ''
    print(
        f"  [mb dbg] {key}: A={A:.1f}nm T={T:.2f}s A/T={A/T:.1f} "
        f"Q={Q:.2f}({dist_deg:.0f}deg{approx_flag}) -> mb={mb:.1f}",
        flush=True,
    )
    return round(mb, 1), ('approx' if approx else None), A


def report_mb_deferred(stations_fired, p_arrivals, epicenter_latlon, det_unix,
                       det_ts=None, det_conf=None):
    """Thread: waits MB_DELAY_S then measures mb; fires Slack alert with result."""
    time.sleep(MB_DELAY_S)
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    mbs = []
    for k in sorted(stations_fired):
        p_t = p_arrivals.get(k)
        if p_t is None:
            continue
        mb, tag, amp = estimate_mb(k, p_t, epicenter_latlon)
        if mb is not None:
            dist_str = ""
            if epicenter_latlon and k in station_coords:
                d = haversine_km(station_coords[k][0], station_coords[k][1],
                                 epicenter_latlon[0], epicenter_latlon[1]) / 111.195
                dist_str = f"  Δ={d:.1f}°"
            flag = " [approx Δ=45°]" if tag == 'approx' else ""
            print(f"  [mb {ts}] {k}: mb={mb:.1f}{dist_str}{flag}", flush=True)
            mbs.append((mb, tag, amp))
        else:
            print(f"  [mb {ts}] {k}: skipped ({tag})", flush=True)

    mb_result = None
    if mbs:
        mb_vals = [m for m, _, _ in mbs]
        tags = [t for _, t, _ in mbs]
        amp_vals = [a for _, _, a in mbs]
        consensus = float(np.median(mb_vals))
        approx = all(t == 'approx' for t in tags)
        # If no epicenter and stations show large amplitude spread, source is likely local/regional
        amp_ratio = max(amp_vals) / min(amp_vals) if len(amp_vals) > 1 and min(amp_vals) > 0 else 1.0
        local_flag = approx and amp_ratio > 5.0
        n = len(mb_vals)
        approx_pfx = '~' if approx else ''
        approx_sfx = ', Δ≈45°' if approx else ''
        local_sfx = f', likely local amp_ratio={amp_ratio:.1f}' if local_flag else ''
        label = f"({approx_pfx}{n} stations, IASPEI{approx_sfx}{local_sfx})"
        print(f"  [mb {ts}] mb={approx_pfx}{consensus:.1f}  {label}", flush=True)
        sensor_state.update_mb(det_unix, consensus, approx=approx, local=local_flag)
        mb_result = consensus

    if det_ts is not None and det_conf is not None:
        from seismic.catalog import send_slack_alert  # noqa: PLC0415
        send_slack_alert(det_ts, stations_fired, det_conf, epicenter_latlon, mb_result)
