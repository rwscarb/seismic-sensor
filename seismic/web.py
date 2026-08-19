import json
import threading
import time

from seismic.config import (
    P_VEL_KM_S,
    WEB_PORT, THRESHOLD, N_CONSENSUS, STATIONS, ALL_STATIONS, SEEDLINK_SERVER,
    USGS_MIN_MAG, EMSC_MIN_MAG, UMAMI_SITE_ID, LOC_MIN_STA, FAULT_GEOJSON_URL,
    CONSENSUS_WINDOW, USGS_SIG_MIN_MAG, SLACK_SIGNING_SECRET,
    SERVER_START_TIME, _LOG_BUF, _LOG_LOCK, MAPBOX_TOKEN, BTCVM_LEDGER_PATH,
    API_KEY,
)
import seismic.runtime as _runtime
from seismic.localize import station_coords, locate_epicenter
from seismic.state import sensor_state
from seismic.watcher import _expected_p_arrival, _find_matching_detection


_ensemble_models = None

def set_ensemble_models(models):
    """Called from sensor.py after load_ensemble() so /api/backfill can use them."""
    global _ensemble_models
    _ensemble_models = models


def start_web_server():
    if WEB_PORT == 0:
        return
    try:
        from flask import Flask, jsonify, render_template
    except ImportError:
        print("flask not installed — web UI disabled (pip install flask)", flush=True)
        return

    coords_json = json.dumps({k: list(v) for k, v in list(station_coords.items())})
    sta_list = ', '.join(f"{n}.{s}" for n, s in STATIONS)
    cfg_text = f"threshold {THRESHOLD} | {N_CONSENSUS}/{len(ALL_STATIONS)} consensus | {CONSENSUS_WINDOW:.0f}s window"
    app_title = f"{sta_list} | fra"

    _recall_cache = {}   # {(days, minmag): (ts, result)}
    _recall_lock  = threading.Lock()
    _RECALL_TTL   = 300.0  # 5 min — USGS fetch is slow; don't hammer on every scoreboard refresh

    app = Flask(__name__)
    import logging
    from flask import request, Response
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    # Simple in-memory per-IP rate limiter: max 60 req/min on API endpoint
    _rate_buckets = {}
    _rate_lock = threading.Lock()
    _RATE_LIMIT = 60    # requests
    _RATE_WINDOW = 60.0  # seconds

    def _check_rate(ip):
        now = time.time()
        with _rate_lock:
            if ip not in _rate_buckets:
                _rate_buckets[ip] = []
            bucket = _rate_buckets[ip]
            # prune old entries
            _rate_buckets[ip] = [t for t in bucket if now - t < _RATE_WINDOW]
            if len(_rate_buckets[ip]) >= _RATE_LIMIT:
                return False
            _rate_buckets[ip].append(now)
            # prune stale IPs periodically
            if len(_rate_buckets) > 500:
                cutoff = now - _RATE_WINDOW
                for k in list(_rate_buckets):
                    if all(t < cutoff for t in _rate_buckets[k]):
                        del _rate_buckets[k]
            return True

    @app.route('/')
    def index():
        state_data = sensor_state.to_dict()
        state_data['station_coords'] = {k: list(v) for k, v in list(station_coords.items())}
        try:
            from seismic.collector import collection_stats  # noqa: PLC0415
            state_data['training'] = collection_stats()
        except Exception:
            pass
        return render_template(
            'index.html',
            app_title=app_title,
            cfg_text=cfg_text,
            seedlink=SEEDLINK_SERVER,
            threshold=THRESHOLD,
            usgs_min_mag=USGS_MIN_MAG,
            emsc_min_mag=EMSC_MIN_MAG,
            umami_id=UMAMI_SITE_ID,
            station_coords_json=coords_json,
            fault_geojson_url=FAULT_GEOJSON_URL,
            mapbox_token=MAPBOX_TOKEN,
            p_vel_km_s=P_VEL_KM_S,
            initial_state_json=json.dumps(state_data),
        )

    @app.route('/health')
    def health():
        # Fail if all known stations have gone silent for >5 minutes
        STALE_S = 300.0
        now = time.time()
        snaps = sensor_state.stations
        if snaps:
            freshest = max(s.last_ts for s in snaps.values())
            if now - freshest > STALE_S:
                age = int(now - freshest)
                return Response(f'stale feeds ({age}s)', status=503, mimetype='text/plain')
        return Response('ok', status=200, mimetype='text/plain')

    @app.route('/api/state')
    def state():
        data = sensor_state.to_dict()
        data['station_coords'] = {k: list(v) for k, v in list(station_coords.items())}
        try:
            from seismic.collector import collection_stats  # noqa: PLC0415
            data['training'] = collection_stats()
        except Exception:
            pass
        return jsonify(data)

    @app.route('/api/btcvm')
    def btcvm_ledger():
        import os
        entries = []
        try:
            if os.path.exists(BTCVM_LEDGER_PATH):
                with open(BTCVM_LEDGER_PATH) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entries.append(json.loads(line))
                            except Exception:
                                pass
        except Exception:
            pass
        return jsonify({'entries': entries[-200:]})

    @app.route('/api/logs')
    def logs():
        since = request.args.get('since', type=int, default=0)
        with _LOG_LOCK:
            entries = list(_LOG_BUF)
        if since:
            entries = [e for e in entries if e.get('seq', 0) > since]
        return jsonify({'entries': entries[-100:], 'total': len(_LOG_BUF)})

    @app.route('/api/scoreboard')
    def scoreboard():
        """Model accuracy scoreboard based on USGS cross-correlation results.

        Query params:
          days (float, default 0 = all-time) — look-back window in days; 0 means all

        A detection is scoreable only when usgs_checked=True.
        - confirmed : usgs is not None  (true positive — USGS found a matching event)
        - false_pos : usgs is None      (no matching catalog event found)

        Returns counts, precision, and per-detection breakdown.
        """
        import time as _time
        days = request.args.get('days', default=0.0, type=float)

        with sensor_state._lock:
            dets = list(sensor_state.detections)

        if days > 0:
            cutoff = _time.time() - days * 86400
            dets = [d for d in dets if (d.unix_ts or 0) >= cutoff]

        checked  = [d for d in dets if d.usgs_checked]
        confirmed = [d for d in checked if d.usgs is not None]
        false_pos = [d for d in checked if d.usgs is None]
        pending   = [d for d in dets if not d.usgs_checked]

        precision = round(len(confirmed) / len(checked), 4) if checked else None

        def _fmt(d):
            row = {
                'ts': d.ts,
                'conf': round(d.conf, 3),
                'mb': d.mb,
                'confirmed': d.usgs is not None,
            }
            if d.usgs:
                row['usgs_mag']   = d.usgs.get('magnitude')
                row['usgs_place'] = d.usgs.get('place')
                row['usgs_id']    = d.usgs.get('id')
            return row

        return jsonify({
            'total_detections': len(dets),
            'checked': len(checked),
            'confirmed': len(confirmed),
            'false_positives': len(false_pos),
            'pending': len(pending),
            'precision': precision,
            'detections': [_fmt(d) for d in sorted(checked, key=lambda x: x.unix_ts, reverse=True)],
        })

    @app.route('/api/recall')
    def recall():
        """Confusion-matrix recall endpoint.

        Query params:
          days   (float, default 7)   — look-back window
          minmag (float, default 4.5) — minimum USGS magnitude to include

        Results are cached for _RECALL_TTL seconds to avoid hammering USGS.
        Stale cache is returned immediately while a background refresh runs,
        so the endpoint never blocks a Flask worker on a live USGS fetch.
        """
        from seismic.watcher import compute_recall_window
        days      = request.args.get('days',      default=7.0,   type=float)
        minmag    = request.args.get('minmag',    default=4.5,   type=float)
        since_restart = request.args.get('since_restart', default='0')
        since_ts  = SERVER_START_TIME if since_restart not in ('0', 'false', '') else None
        cache_key = (days, minmag, bool(since_ts))
        now = time.time()

        with _recall_lock:
            cached = _recall_cache.get(cache_key)

        # Return cached result immediately (even if stale) and refresh in background
        if cached:
            age = now - cached[0]
            if age > _RECALL_TTL:
                # Kick off background refresh without blocking the response
                def _bg_refresh():
                    try:
                        result = compute_recall_window(days=days, minmag=minmag, since_ts=since_ts)
                        if 'error' not in result:
                            with _recall_lock:
                                _recall_cache[cache_key] = (time.time(), result)
                    except Exception:
                        pass
                threading.Thread(target=_bg_refresh, daemon=True).start()
            return jsonify(cached[1])

        # First-ever fetch for this key — run synchronously but with short timeout guard
        result = compute_recall_window(days=days, minmag=minmag, since_ts=since_ts)
        if 'error' in result:
            return jsonify(result), 502
        with _recall_lock:
            _recall_cache[cache_key] = (time.time(), result)
        return jsonify(result)

    @app.route('/api/localize', methods=['POST'])
    def localize():
        """Compute epicenter from station arrival times.

        Body (JSON):
          {"arrivals": [["NET.STA", unix_ts], ...]}

        Returns:
          {"lat": float, "lon": float, "rms": float, "n": int}
          or {"error": "..."} on failure.
        """
        try:
            body = request.get_json(force=True)
            arrivals = [(str(k), float(t)) for k, t in body.get('arrivals', [])]
        except Exception as e:
            return jsonify({'error': f'bad request: {e}'}), 400
        if len(arrivals) < LOC_MIN_STA:
            return jsonify({'error': f'need at least {LOC_MIN_STA} arrivals, got {len(arrivals)}'}), 422
        result = locate_epicenter(arrivals)
        if result is None:
            return jsonify({'error': 'localization failed (optimizer did not converge)'}), 422
        lat, lon, rms = result
        return jsonify({'lat': round(lat, 4), 'lon': round(lon, 4), 'rms': round(rms, 3), 'n': len(arrivals)})

    # ── Slack slash command endpoint ───────────────────────────────────────────
    @app.route('/slack/command', methods=['POST'])
    def slack_command():
        import hashlib
        import hmac
        # Verify Slack request signature
        if SLACK_SIGNING_SECRET:
            ts = request.headers.get('X-Slack-Request-Timestamp', '')
            sig = request.headers.get('X-Slack-Signature', '')
            body_bytes = request.get_data()
            base = f'v0:{ts}:{body_bytes.decode()}'.encode()
            expected = 'v0=' + hmac.new(
                SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, sig):
                return Response('invalid signature', status=403, mimetype='text/plain')
        else:
            body_bytes = request.get_data()

        text = request.form.get('text', '').strip().lower()
        parts = text.split()
        sub = parts[0] if parts else 'status'
        now = time.time()

        if sub in ('status', ''):
            snap = sensor_state.to_dict()
            uptime = int(now - SERVER_START_TIME)
            h, m = divmod(uptime // 60, 60)
            sta_lines = []
            for key, s in sorted(snap['stations'].items()):
                age = int(now - s['last_ts'])
                sta_lines.append(f"`{key}` conf={s['conf']:.3f} age={age}s")
            det_count = len(snap['detections'])
            last_det = snap['detections'][-1]['ts'] if snap['detections'] else 'none'
            sta_text = '*Stations:*\n' + '\n'.join(sta_lines) if sta_lines else '*Stations:* none active'
            rt = _runtime.status_dict()
            if rt['muted']:
                rem = rt['muted_remaining_s']
                rm, rs = divmod(rem, 60)
                mute_text = f'🔇 *Muted* — {rm}m {rs}s remaining'
            else:
                mute_text = '🔔 Alerts active'
            thr_tag = ' *(override)*' if rt['threshold_override'] else ''
            blocks = [
                {'type': 'header', 'text': {'type': 'plain_text', 'text': '🌍 Seismic Sensor Status'}},
                {'type': 'section', 'fields': [
                    {'type': 'mrkdwn', 'text': f'*Uptime:* {h}h {m}m'},
                    {'type': 'mrkdwn', 'text': f'*Detections:* {det_count} total'},
                    {'type': 'mrkdwn', 'text': f'*Last detection:* {last_det}'},
                    {'type': 'mrkdwn', 'text': f'*Threshold:* `{rt["threshold"]:.3f}`{thr_tag}'},
                    {'type': 'mrkdwn', 'text': mute_text},
                ]},
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': sta_text}},
            ]
            return jsonify({'response_type': 'in_channel', 'blocks': blocks})

        elif sub == 'recent':
            n = 5
            if len(parts) > 1 and parts[1].isdigit():
                n = min(int(parts[1]), 20)
            dets = sensor_state.to_dict()['detections'][-n:]
            if not dets:
                return jsonify({'response_type': 'in_channel', 'text': 'No detections on record.'})
            lines = []
            for d in reversed(dets):
                mb_str = f"mb={d['mb']:.1f}" if d.get('mb') is not None else 'mb=?'
                epi_str = ''
                if d.get('epicenter'):
                    lat, lon = d['epicenter']
                    ns = 'N' if lat >= 0 else 'S'
                    ew = 'E' if lon >= 0 else 'W'
                    epi_str = f" | {abs(lat):.1f}°{ns} {abs(lon):.1f}°{ew}"
                usgs_str = ''
                if d.get('usgs'):
                    u = d['usgs']
                    usgs_str = f" → M{u['mag']} {u['place']}"
                lines.append(f"`{d['ts']}` {mb_str} conf={d['conf']:.3f}{epi_str}{usgs_str}")
            return jsonify({
                'response_type': 'in_channel',
                'text': f'*Last {len(dets)} detections:*\n' + '\n'.join(lines),
            })

        elif sub == 'usgs':
            # Show recent events from the sig-watcher's seen set
            import urllib.request as ureq
            try:
                url = (
                    f'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
                    f'&minmagnitude={USGS_SIG_MIN_MAG}'
                    f'&orderby=time-asc'
                    f'&starttime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))}'
                    f'&endtime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}'
                    f'&limit=10'
                )
                with ureq.urlopen(url, timeout=15) as resp:
                    data = json.loads(resp.read())
                feats = data.get('features', [])[-8:]
                if not feats:
                    return jsonify({
                        'response_type': 'in_channel',
                        'text': f'No M{USGS_SIG_MIN_MAG}+ events in past 24h.',
                    })
                lines = []
                for f in reversed(feats):
                    p = f['properties']
                    c = f['geometry']['coordinates']
                    ts = time.strftime('%H:%MZ', time.gmtime(p['time'] / 1000))
                    exp = _expected_p_arrival(c[1], c[0], p['time'] / 1000)
                    matched = _find_matching_detection(exp)
                    status = '✅' if matched else '❌'
                    lines.append(f"{status} `{ts}` M{p['mag']} {p.get('place', '?')}")
                return jsonify({
                    'response_type': 'in_channel',
                    'text': (
                        f'*M{USGS_SIG_MIN_MAG}+ events past 24h (✅=detected ❌=missed):*\n'
                        + '\n'.join(lines)
                    ),
                })
            except Exception as e:
                return jsonify({'response_type': 'ephemeral', 'text': f'USGS fetch failed: {e}'})

        elif sub == 'mute':
            # /seismic mute [minutes]   default = 60
            duration = 60.0
            if len(parts) > 1:
                try:
                    duration = float(parts[1])
                    if duration <= 0:
                        return jsonify({'response_type': 'ephemeral',
                                        'text': 'Duration must be > 0 minutes.'})
                    duration = min(duration, 1440)  # cap at 24 h
                except ValueError:
                    return jsonify({'response_type': 'ephemeral',
                                    'text': 'Usage: `/seismic mute [minutes]`'})
            user_id = request.form.get('user_id', '')
            user_name = request.form.get('user_name', 'unknown')
            _runtime.mute(duration, by=f'<@{user_id}>' if user_id else user_name)
            until_str = time.strftime('%H:%MZ', time.gmtime(time.time() + duration * 60))
            return jsonify({
                'response_type': 'in_channel',
                'text': f'🔇 Seismic alerts muted for *{duration:.0f} min* (until `{until_str}`).',
            })

        elif sub == 'unmute':
            user_id = request.form.get('user_id', '')
            user_name = request.form.get('user_name', 'unknown')
            _runtime.unmute(by=f'<@{user_id}>' if user_id else user_name)
            return jsonify({'response_type': 'in_channel', 'text': '🔔 Seismic alerts unmuted.'})

        elif sub == 'sensitivity':
            # /seismic sensitivity <0.0–1.0 | reset>
            if len(parts) < 2:
                rt = _runtime.status_dict()
                cur = rt['threshold']
                tag = ' *(override)*' if rt['threshold_override'] else ' *(default)*'
                return jsonify({'response_type': 'ephemeral',
                                'text': f'Current threshold: `{cur:.3f}`{tag}\nUsage: `/seismic sensitivity <0.0–1.0 | reset>`'})
            arg = parts[1]
            if arg == 'reset':
                _runtime.reset_threshold()
                return jsonify({'response_type': 'in_channel',
                                'text': f'↩️ Detection threshold reset to default (`{THRESHOLD}`).' })
            try:
                val = float(arg)
                if not 0.0 <= val <= 1.0:
                    raise ValueError
            except ValueError:
                return jsonify({'response_type': 'ephemeral',
                                'text': 'Threshold must be a float between 0.0 and 1.0, or `reset`.'})
            _runtime.set_threshold(val)
            direction = '📈 more sensitive' if val < THRESHOLD else '📉 less sensitive'
            return jsonify({
                'response_type': 'in_channel',
                'text': f'🎚️ Detection threshold set to `{val:.3f}` ({direction}; default `{THRESHOLD}`).',
            })

        elif sub == 'help':
            return jsonify({'response_type': 'ephemeral', 'text': (
                '*Seismic Sensor slash commands:*\n'
                '`/seismic status` — station health, uptime, detection count\n'
                '`/seismic recent [N]` — last N detections (default 5, max 20)\n'
                '`/seismic usgs` — M5.5+ events past 24h with detection status\n'
                '`/seismic mute [minutes]` — silence Slack alerts (default 60 min, max 1440)\n'
                '`/seismic unmute` — re-enable alerts immediately\n'
                '`/seismic sensitivity <0.0–1.0 | reset>` — adjust detection threshold\n'
                '`/seismic help` — this message'
            )})

        else:
            return jsonify({
                'response_type': 'ephemeral',
                'text': f'Unknown subcommand `{sub}`. Try `/seismic help`.',
            })

    def _require_api_key():
        """Returns a 401 Response if API_KEY is set and the request doesn't supply it.
        Returns None if auth passes (key not configured, or correct key supplied).
        Accepts key via X-API-Key header or ?api_key= query param.
        """
        if not API_KEY:
            return None  # auth disabled
        supplied = request.headers.get('X-API-Key') or request.args.get('api_key', '')
        if supplied == API_KEY:
            return None
        return Response(
            json.dumps({'error': 'unauthorized', 'hint': 'supply X-API-Key header or ?api_key='}),
            status=401, mimetype='application/json',
        )

    @app.route('/api/detections')
    def api_detections():
        """Paginated, filterable detection list.

        Query params:
          limit   (int,   default 50, max 500)
          offset  (int,   default 0)
          days    (float, default 0 = all-time)
          minconf (float, default 0.0)
          confirmed (bool: true/false, default unfiltered)
        """
        auth_err = _require_api_key()
        if auth_err:
            return auth_err

        limit   = min(request.args.get('limit',   default=50,  type=int), 500)
        offset  = request.args.get('offset',  default=0,   type=int)
        days    = request.args.get('days',    default=0.0, type=float)
        minconf = request.args.get('minconf', default=0.0, type=float)
        confirmed_filter = request.args.get('confirmed', default=None)

        with sensor_state._lock:
            dets = list(sensor_state.detections)

        if days > 0:
            cutoff = time.time() - days * 86400
            dets = [d for d in dets if (d.unix_ts or 0) >= cutoff]
        if minconf > 0:
            dets = [d for d in dets if d.conf >= minconf]
        if confirmed_filter == 'true':
            dets = [d for d in dets if d.usgs_checked and d.usgs is not None]
        elif confirmed_filter == 'false':
            dets = [d for d in dets if d.usgs_checked and d.usgs is None]

        dets = list(reversed(dets))  # newest first
        total = len(dets)
        page  = dets[offset:offset + limit]

        def _fmt(d):
            row = {
                'ts':          d.ts,
                'unix_ts':     d.unix_ts,
                'conf':        round(d.conf, 4),
                'mb':          d.mb,
                'mb_approx':   d.mb_approx,
                'mb_local':    d.mb_local,
                'stations':    list(d.stations),
                'epicenter':   list(d.epicenter) if d.epicenter else None,
                'teleseismic': d.teleseismic,
                'usgs_checked': d.usgs_checked,
                'usgs':        d.usgs,
            }
            return row

        return jsonify({
            'total':   total,
            'offset':  offset,
            'limit':   limit,
            'detections': [_fmt(d) for d in page],
        })

    @app.route('/api/stations')
    def api_stations():
        """Current per-station snapshot: conf, mag estimate, last seen, flatline status."""
        auth_err = _require_api_key()
        if auth_err:
            return auth_err

        now  = time.time()
        snap = sensor_state.to_dict()
        rows = {}
        for key, s in snap.get('stations', {}).items():
            age = round(now - s['last_ts'], 1) if s.get('last_ts') else None
            rows[key] = {
                'conf':      round(s['conf'], 4),
                'mag_est':   s.get('mag_est'),
                'last_ts':   s.get('last_ts'),
                'age_s':     age,
                'flatline':  s.get('flatline', False),
                'coords':    list(station_coords[key]) if key in station_coords else None,
            }
        return jsonify({'stations': rows, 'count': len(rows)})

    @app.route('/api/config')
    def api_config():
        """Read-only view of current runtime config and thresholds."""
        auth_err = _require_api_key()
        if auth_err:
            return auth_err

        rt = _runtime.status_dict()
        return jsonify({
            'threshold':          rt['threshold'],
            'threshold_override': rt['threshold_override'],
            'threshold_default':  THRESHOLD,
            'n_consensus':        N_CONSENSUS,
            'consensus_window_s': CONSENSUS_WINDOW,
            'usgs_min_mag':       USGS_MIN_MAG,
            'emsc_min_mag':       EMSC_MIN_MAG,
            'stations':           [f'{n}.{s}' for n, s in STATIONS],
            'station_count':      len(ALL_STATIONS),
            'seedlink':           SEEDLINK_SERVER,
            'muted':              rt['muted'],
            'muted_remaining_s':  rt.get('muted_remaining_s'),
            'uptime_s':           round(time.time() - SERVER_START_TIME),
        })

    @app.route('/api/backfill')
    def api_backfill():
        """Fetch historical waveforms and simulate detection for a past event.
        Query params: lat, lon, time (ISO UTC or unix), label (optional)
        e.g. /api/backfill?lat=-8.27&lon=121.51&time=2026-08-16T00:46:00Z
        """
        if _ensemble_models is None:
            return jsonify({'error': 'models not loaded yet'}), 503
        try:
            lat   = float(request.args['lat'])
            lon   = float(request.args['lon'])
            label = request.args.get('label', '')
            t_raw = request.args.get('time', '')
            if not t_raw:
                return jsonify({'error': 'missing ?time='}), 400
            try:
                origin_unix = float(t_raw)
            except ValueError:
                from datetime import datetime, timezone
                origin_unix = datetime.strptime(
                    t_raw.replace('Z', '+00:00'), '%Y-%m-%dT%H:%M:%S%z'
                ).replace(tzinfo=timezone.utc).timestamp()
        except (KeyError, ValueError) as e:
            return jsonify({'error': f'bad params: {e}'}), 400
        from seismic.backfill import evaluate_event
        result = evaluate_event(lat, lon, origin_unix, _ensemble_models, label)
        return jsonify(result)

    @app.route('/api/station_log')
    def api_station_log():
        """Persisted per-station confidence readings for a time range — for
        diagnosing a missed detection after the fact. Fly's own log buffer
        only retains a couple minutes; this survives restarts and lasts
        STATION_LOG_RETENTION_DAYS (default 3).

        Query params:
          start    (unix or ISO UTC, required)
          end      (unix or ISO UTC, default start + 3600)
          station  (optional, e.g. GE.APE — filters to one station)
        """
        from seismic.station_log import read_range

        def _parse_time(raw):
            try:
                return float(raw)
            except ValueError:
                from datetime import datetime, timezone
                return datetime.strptime(
                    raw.replace('Z', '+00:00'), '%Y-%m-%dT%H:%M:%S%z'
                ).replace(tzinfo=timezone.utc).timestamp()

        start_raw = request.args.get('start', '')
        if not start_raw:
            return jsonify({'error': 'missing ?start='}), 400
        try:
            start = _parse_time(start_raw)
            end = _parse_time(request.args['end']) if 'end' in request.args else start + 3600
        except ValueError as e:
            return jsonify({'error': f'bad time: {e}'}), 400

        station = request.args.get('station', '')
        rows = read_range(start, end)
        if station:
            rows = [r for r in rows if r['station'] == station]
        return jsonify({'start': start, 'end': end, 'count': len(rows), 'rows': rows})

    t = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=WEB_PORT, threaded=True),
        daemon=True,
        name='web-ui',
    )
    t.start()
    print(f"Web UI: http://0.0.0.0:{WEB_PORT}", flush=True)
