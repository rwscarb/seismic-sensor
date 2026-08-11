"""
btcvm_anchor.py — Anchor seismic detections to Bitcoin blocks.

Commitment scheme mirrors btcvm v1:
  det_hash   = SHA256(canonical_json_of_detection)
  block_hash = latest Bitcoin block hash (blockstream.info)
  commitment = SHA256(block_hash + det_hash)

Each anchor is appended to a JSONL ledger (BTCVM_LEDGER_PATH).
Independent verification:
  1. Fetch block_hash from any Bitcoin node or blockstream.info by height
  2. Recompute det_hash from the detection record
  3. Recompute commitment and compare

Optional OP_RETURN broadcast requires the 'bit' library and BTC_WIF env var.
"""

import hashlib
import json
import threading
import time
import urllib.request

from seismic.config import BTCVM_LEDGER_PATH, BTCVM_BROADCAST, BTC_WIF


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch_tip() -> tuple[str, int]:
    """Return (block_hash, block_height) for the latest Bitcoin block."""
    with urllib.request.urlopen(
        'https://blockstream.info/api/blocks/tip/hash', timeout=10
    ) as r:
        block_hash = r.read().decode().strip()
    with urllib.request.urlopen(
        'https://blockstream.info/api/blocks/tip/height', timeout=10
    ) as r:
        block_height = int(r.read().decode().strip())
    return block_hash, block_height


def _broadcast_op_return(commitment_hex: str) -> str | None:
    """Broadcast commitment as OP_RETURN to Bitcoin. Returns tx hash or None."""
    if not BTC_WIF:
        return None
    try:
        from bit import PrivateKey  # type: ignore
        key = PrivateKey(BTC_WIF)
        # OP_RETURN payload: first 40 bytes of commitment (80 hex chars = 40 bytes)
        payload = bytes.fromhex(commitment_hex[:80])
        outputs = [(payload, 0, 'satoshi')]
        tx_hash = key.send(outputs)
        return tx_hash
    except Exception as e:
        print(f"  [btcvm] OP_RETURN broadcast failed: {e}", flush=True)
        return None


def _anchor(det_data: dict, label: str) -> dict | None:
    """
    Core anchoring logic. det_data must be JSON-serialisable.
    label: 'raw' | 'confirmed'
    """
    import os
    try:
        canonical = json.dumps(det_data, sort_keys=True, separators=(',', ':')).encode()
        det_hash = _sha256(canonical)
        block_hash, block_height = _fetch_tip()
        commitment = _sha256((block_hash + det_hash).encode())

        tx_hash = None
        if BTCVM_BROADCAST:
            tx_hash = _broadcast_op_return(commitment)

        entry = {
            'label': label,
            'det_ts': det_data.get('ts'),
            'det_unix': det_data.get('unix_ts'),
            'det_hash': det_hash,
            'block_height': block_height,
            'block_hash': block_hash,
            'commitment': commitment,
            'anchored_at': time.time(),
        }
        if tx_hash:
            entry['tx_hash'] = tx_hash

        os.makedirs(os.path.dirname(BTCVM_LEDGER_PATH), exist_ok=True)
        with open(BTCVM_LEDGER_PATH, 'a') as f:
            f.write(json.dumps(entry) + '\n')

        print(
            f"  [btcvm] {label} anchor @ block {block_height} "
            f"commitment={commitment[:16]}...",
            flush=True,
        )
        return entry
    except Exception as e:
        print(f"  [btcvm] anchor failed ({label}): {e}", flush=True)
        return None


def anchor_detection(det_rec) -> None:
    """
    Fire-and-forget: anchor a raw detection record to the latest Bitcoin block.
    det_rec is a DetectionSnap (has .ts, .unix_ts, .stations, .conf, .epicenter).
    Runs in a background thread; never blocks the detection pipeline.
    """
    data = {
        'ts': det_rec.ts,
        'unix_ts': det_rec.unix_ts,
        'stations': list(det_rec.stations),
        'conf': round(det_rec.conf, 6),
        'epicenter': list(det_rec.epicenter) if det_rec.epicenter else None,
        'teleseismic': det_rec.teleseismic,
    }
    threading.Thread(
        target=_anchor, args=(data, 'raw'), daemon=True, name='btcvm-raw'
    ).start()


def anchor_confirmed(det_unix: float, catalog_event: dict) -> None:
    """
    Fire-and-forget: anchor a catalog-confirmed detection to the latest Bitcoin block.
    Includes USGS/EMSC event data for a richer commitment.
    Runs in a background thread; never blocks the catalog pipeline.
    """
    from seismic.state import sensor_state  # avoid circular import
    det = sensor_state.get_detection(det_unix)
    if det is None:
        return
    data = {
        'ts': det.ts,
        'unix_ts': det.unix_ts,
        'stations': list(det.stations),
        'conf': round(det.conf, 6),
        'epicenter': list(det.epicenter) if det.epicenter else None,
        'teleseismic': det.teleseismic,
        'catalog': {
            'source': catalog_event.get('source', 'usgs'),
            'mag': catalog_event.get('mag'),
            'magType': catalog_event.get('magType'),
            'place': catalog_event.get('place'),
            'lat': catalog_event.get('lat'),
            'lon': catalog_event.get('lon'),
            'depth': catalog_event.get('depth'),
            'event_id': catalog_event.get('event_id'),
            'time': catalog_event.get('time'),
        },
    }
    threading.Thread(
        target=_anchor, args=(data, 'confirmed'), daemon=True, name='btcvm-confirmed'
    ).start()
