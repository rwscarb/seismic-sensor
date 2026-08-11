"""
btcvm_anchor.py — Anchor seismic detections to Bitcoin blocks.

Commitment scheme:
  Individual detections:
    det_hash   = SHA256(canonical_json_of_detection)
    block_hash = latest Bitcoin block hash at time of detection
    commitment = SHA256(block_hash + det_hash)
    → written to JSONL ledger; no on-chain broadcast

  Daily batch (midnight UTC):
    merkle_root = binary Merkle tree of all det_hashes since last batch
    block_hash  = Bitcoin tip at batch time
    commitment  = SHA256(block_hash + merkle_root)
    → OP_RETURN broadcast (one tx/day); batch entry written to ledger

Independent verification of any detection:
  1. Find its det_hash in the ledger (label='raw' or 'confirmed')
  2. Find the batch entry (label='batch') that includes it
  3. Recompute merkle_root from the batch's det_hashes list
  4. Recompute commitment = SHA256(block_hash + merkle_root)
  5. Compare against the on-chain OP_RETURN at batch_tx_hash
"""

import hashlib
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

from seismic.config import BTCVM_LEDGER_PATH, BTCVM_BROADCAST, BTC_WIF


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _merkle_root(hashes: list[str]) -> str:
    """Binary Merkle root of a list of hex-encoded SHA256 hashes."""
    if not hashes:
        return _sha256(b'empty')
    layer = list(hashes)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [_sha256((layer[i] + layer[i + 1]).encode()) for i in range(0, len(layer), 2)]
    return layer[0]


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
        tx_hash = key.send([], message=commitment_hex[:80])
        return tx_hash
    except Exception as e:
        print(f"  [btcvm] OP_RETURN broadcast failed: {e}", flush=True)
        return None


def _read_ledger() -> list[dict]:
    """Read all JSONL entries from the ledger. Returns [] if missing."""
    entries = []
    try:
        if not os.path.exists(BTCVM_LEDGER_PATH):
            return []
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
    return entries


def _append_ledger(entry: dict) -> None:
    os.makedirs(os.path.dirname(BTCVM_LEDGER_PATH), exist_ok=True)
    with open(BTCVM_LEDGER_PATH, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def _anchor(det_data: dict, label: str) -> dict | None:
    """
    Write a detection anchor to the ledger. No on-chain broadcast.
    label: 'raw' | 'confirmed'
    """
    try:
        canonical = json.dumps(det_data, sort_keys=True, separators=(',', ':')).encode()
        det_hash = _sha256(canonical)
        block_hash, block_height = _fetch_tip()
        commitment = _sha256((block_hash + det_hash).encode())

        entry = {
            'scheme': 'v2-single',
            'label': label,
            'det_ts': det_data.get('ts'),
            'det_unix': det_data.get('unix_ts'),
            'det_hash': det_hash,
            'block_height': block_height,
            'block_hash': block_hash,
            'commitment': commitment,
            'anchored_at': time.time(),
        }
        _append_ledger(entry)
        print(
            f"  [btcvm] {label} anchor @ block {block_height} "
            f"commitment={commitment[:16]}...",
            flush=True,
        )
        return entry
    except Exception as e:
        print(f"  [btcvm] anchor failed ({label}): {e}", flush=True)
        return None


def _daily_batch() -> None:
    """
    Collect all unmatched det_hashes since the last batch, build a Merkle tree,
    commit to Bitcoin via OP_RETURN, and write a batch entry to the ledger.
    """
    try:
        entries = _read_ledger()
        # find timestamp of last batch (if any)
        batch_entries = [e for e in entries if e.get('label') == 'batch']
        last_batch_at = batch_entries[-1]['anchored_at'] if batch_entries else 0.0

        # collect det_hashes from raw/confirmed entries added since last batch
        det_entries = [
            e for e in entries
            if e.get('label') in ('raw', 'confirmed')
            and e.get('anchored_at', 0) > last_batch_at
            and e.get('det_hash')
        ]
        if not det_entries:
            print('  [btcvm] daily batch: no new detections, skipping', flush=True)
            return

        det_hashes = [e['det_hash'] for e in det_entries]
        root = _merkle_root(det_hashes)
        block_hash, block_height = _fetch_tip()
        commitment = _sha256((block_hash + root).encode())

        tx_hash = None
        if BTCVM_BROADCAST:
            tx_hash = _broadcast_op_return(commitment)
            if tx_hash:
                print(f"  [btcvm] OP_RETURN ok: {tx_hash}", flush=True)

        batch_entry = {
            'scheme': 'v2-batch-merkle',
            'label': 'batch',
            'det_hashes': det_hashes,
            'merkle_root': root,
            'block_height': block_height,
            'block_hash': block_hash,
            'commitment': commitment,
            'anchored_at': time.time(),
            'n': len(det_hashes),
        }
        if tx_hash:
            batch_entry['tx_hash'] = tx_hash

        _append_ledger(batch_entry)
        print(
            f"  [btcvm] daily batch: {len(det_hashes)} detections"
            f" merkle={root[:16]}..."
            f" @ block {block_height}",
            flush=True,
        )
    except Exception as e:
        print(f"  [btcvm] daily batch failed: {e}", flush=True)


def _batch_scheduler() -> None:
    """Background thread: fire _daily_batch() at midnight UTC each day."""
    while True:
        now = time.time()
        dt = datetime.fromtimestamp(now, tz=timezone.utc)
        # seconds until next midnight UTC
        seconds_until_midnight = (
            (23 - dt.hour) * 3600
            + (59 - dt.minute) * 60
            + (60 - dt.second)
        )
        time.sleep(seconds_until_midnight)
        threading.Thread(target=_daily_batch, daemon=True, name='btcvm-batch').start()
        time.sleep(60)  # avoid double-firing within the same minute


def start_batch_scheduler() -> None:
    """Start the daily batch scheduler. Call once at app startup."""
    threading.Thread(target=_batch_scheduler, daemon=True, name='btcvm-scheduler').start()


def anchor_detection(det_rec) -> None:
    """
    Fire-and-forget: anchor a raw detection record to the ledger.
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
    Fire-and-forget: anchor a catalog-confirmed detection to the ledger.
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
