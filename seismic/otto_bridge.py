"""
otto_bridge.py — bridge between seismic-sensor detections and the otto network.

Reads OTTO_* env vars. If not configured, does nothing silently.
"""

import asyncio
import logging
import os
import threading

logger = logging.getLogger('otto_bridge')

# ── Config from env ────────────────────────────────────────────────────────────
OTTO_ENABLED  = os.environ.get('OTTO_ENABLED', '').lower() in ('1', 'true', 'yes')
OTTO_PRIVKEY  = os.environ.get('OTTO_PRIVKEY', '')     # hex nsec
OTTO_PUBKEY   = os.environ.get('OTTO_PUBKEY', '')      # hex npub
OTTO_LAT      = float(os.environ.get('OTTO_LAT', '0'))
OTTO_LON      = float(os.environ.get('OTTO_LON', '0'))
OTTO_RELAYS   = [r.strip() for r in os.environ.get(
    'OTTO_RELAYS',
    'wss://relay.damus.io,wss://relay.nostr.band,wss://nostr.wine'
).split(',') if r.strip()]
OTTO_STATION  = os.environ.get('OTTO_STATION', '')     # e.g. "GE.MATE"

# ── Lazy init ──────────────────────────────────────────────────────────────────
_node = None
_loop = None
_thread = None


def _get_node():
    global _node, _loop, _thread
    if _node is not None:
        return _node
    if not OTTO_ENABLED or not OTTO_PRIVKEY or not OTTO_PUBKEY:
        return None

    try:
        import sys, os as _os
        # otto may live alongside seismic-sensor or be installed as a package
        _otto_path = _os.path.join(_os.path.dirname(__file__), '..', '..', 'otto')
        if _otto_path not in sys.path:
            sys.path.insert(0, _otto_path)

        from otto.node import OttoNode, NodeConfig
        config = NodeConfig(
            node_id = OTTO_PUBKEY,
            privkey = OTTO_PRIVKEY,
            lat     = OTTO_LAT,
            lon     = OTTO_LON,
            relays  = OTTO_RELAYS,
            station = OTTO_STATION or None,
        )
        _node = OttoNode(config)

        # Run the async publish loop in a dedicated background thread
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(target=_loop.run_forever, daemon=True, name='otto-publish')
        _thread.start()
        asyncio.run_coroutine_threadsafe(_node.run(), _loop)

        logger.info(f'otto bridge active — node {OTTO_PUBKEY[:16]}... '
                    f'publishing to {len(OTTO_RELAYS)} relay(s)')
        return _node
    except Exception as e:
        logger.warning(f'otto bridge init failed: {e}')
        return None


def publish_detection(p_arrival: float, conf: float, mag_est: float,
                      sig_hash: str = None) -> None:
    """
    Call from _fire_detection() to publish to the otto network.
    No-op if OTTO_ENABLED is not set or init failed.
    """
    node = _get_node()
    if node is None:
        return
    try:
        node.on_detection(
            p_arrival = p_arrival,
            conf      = conf,
            mag_est   = mag_est,
            sig_hash  = sig_hash,
        )
    except Exception as e:
        logger.warning(f'otto publish failed: {e}')
