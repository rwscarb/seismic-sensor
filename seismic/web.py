import json
import threading
import time

from seismic.config import (
    WEB_PORT, THRESHOLD, N_CONSENSUS, STATIONS, ALL_STATIONS, SEEDLINK_SERVER,
    USGS_MIN_MAG, EMSC_MIN_MAG, UMAMI_SITE_ID, LOC_MIN_STA,
    CONSENSUS_WINDOW, USGS_SIG_MIN_MAG, SLACK_SIGNING_SECRET,
    SERVER_START_TIME, _LOG_BUF, _LOG_LOCK, MAPBOX_TOKEN, BTCVM_LEDGER_PATH,
)
from seismic.localize import station_coords, locate_epicenter
from seismic.state import sensor_state
from seismic.watcher import _expected_p_arrival, _find_matching_detection


def start_web_server():
    if WEB_PORT == 0:
        return
    try:
        from flask import Flask, jsonify, render_template
    except ImportError:
        print("flask not installed — web UI disabled (pip install flask)", flush=True)
        return

    coords_json = json.dumps({k: list(v) for k, v in station_coords.items()})
    sta_list = ', '.join(f"{n}.{s}" for n, s in STATIONS)
    cfg_text = f"threshold {THRESHOLD} | {N_CONSENSUS}/{len(ALL_STATIONS)} consensus | {CONSENSUS_WINDOW:.0f}s window"
    app_title = f"{sta_list} | fra"

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
            mapbox_token=MAPBOX_TOKEN,
        )

    @app.route('/health')
    def health():
        return Response('ok', status=200, mimetype='text/plain')

    @app.route('/api/state')
    def state():
        data = sensor_state.to_dict()
        data['station_coords'] = {k: list(v) for k, v in station_coords.items()}
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
            blocks = [
                {'type': 'header', 'text': {'type': 'plain_text', 'text': '🌍 Seismic Sensor Status'}},
                {'type': 'section', 'fields': [
                    {'type': 'mrkdwn', 'text': f'*Uptime:* {h}h {m}m'},
                    {'type': 'mrkdwn', 'text': f'*Detections:* {det_count} total'},
                    {'type': 'mrkdwn', 'text': f'*Last detection:* {last_det}'},
                    {'type': 'mrkdwn', 'text': f'*Threshold:* {THRESHOLD}'},
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

        elif sub == 'help':
            return jsonify({'response_type': 'ephemeral', 'text': (
                '*Seismic Sensor slash commands:*\n'
                '`/seismic status` — station health, uptime, detection count\n'
                '`/seismic recent [N]` — last N detections (default 5, max 20)\n'
                '`/seismic usgs` — M5.5+ events past 24h with detection status\n'
                '`/seismic help` — this message'
            )})

        else:
            return jsonify({
                'response_type': 'ephemeral',
                'text': f'Unknown subcommand `{sub}`. Try `/seismic help`.',
            })

    t = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=WEB_PORT, threaded=True),
        daemon=True,
        name='web-ui',
    )
    t.start()
    print(f"Web UI: http://0.0.0.0:{WEB_PORT}", flush=True)
