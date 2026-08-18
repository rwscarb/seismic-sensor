#!/usr/bin/env python3
"""
Fine-tune the StreamingNet ensemble on station-specific labeled data.

Usage:
  python train.py [--data /data/training] [--checkpoints ./checkpoints]
                  [--epochs 30] [--lr 1e-4] [--batch 32] [--seeds 3]
                  [--min-samples 50] [--eval-split 0.2]

Input (from collector.py):
  /data/training/{unix_ts}_{station}_{label}.npz
  Labels: positive (real event), negative (false positive), noise (quiet period)
  negative + noise both = class 0; positive = class 1

Output:
  Overwrites checkpoints/seed_{n}.pt with fine-tuned weights.
  Saves backup to checkpoints/seed_{n}_pretrain.pt before overwriting.
  Prints precision/recall/F1 on held-out eval split.
"""
import argparse
import os
import sys
import glob
import shutil
import numpy as np

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='Fine-tune StreamingNet on labeled station data')
parser.add_argument('--data',        default=os.environ.get('TRAINING_DIR', '/data/training'))
parser.add_argument('--checkpoints', default=os.environ.get('CHECKPOINT_DIR', './checkpoints'))
parser.add_argument('--epochs',      type=int,   default=30)
parser.add_argument('--lr',          type=float, default=1e-4)
parser.add_argument('--batch',       type=int,   default=32)
parser.add_argument('--seeds',       type=int,   default=3)
parser.add_argument('--min-samples', type=int,   default=50,
                    help='Abort if fewer than this many labeled samples exist')
parser.add_argument('--eval-split',  type=float, default=0.2)
parser.add_argument('--sample-power', type=float, default=0.5,
                    help='Class-balance sampler exponent: 1.0 = full inverse-frequency '
                         '(trains on an artificial ~50/50 world, tends to tank precision '
                         'at real-world imbalance), 0.0 = no rebalancing at all. Default 0.5 '
                         '(sqrt) softens the oversampling instead of fully flattening it.')
parser.add_argument('--eval-threshold', type=float,
                    default=float(os.environ.get('THRESHOLD', '0.5')),
                    help='Report precision/recall/F1 at this softmax-probability cutoff too, '
                         'in addition to the default argmax (0.5) split — argmax alone can '
                         'look far worse than the model actually behaves at the threshold '
                         'that is really used in production.')
parser.add_argument('--dry-run',     action='store_true',
                    help='Load data and print stats; do not train or overwrite checkpoints')
parser.add_argument('--no-backup',   action='store_true',
                    help='Skip checkpoint backup before overwriting')
args = parser.parse_args()

# ── Load dataset ──────────────────────────────────────────────────────────────
print(f'\n=== StreamingNet fine-tune ===')
print(f'Data:        {args.data}')
print(f'Checkpoints: {args.checkpoints}')

files = glob.glob(os.path.join(args.data, '*.npz'))
if not files:
    print(f'ERROR: no .npz files found in {args.data}')
    sys.exit(1)

windows, labels, metas = [], [], []
label_counts = {'positive': 0, 'negative': 0, 'noise': 0, 'pending': 0, 'other': 0}

for fpath in files:
    try:
        d = np.load(fpath, allow_pickle=True)
        raw = d['label']
        lbl = raw.item().decode() if isinstance(raw.item(), bytes) else str(raw.item())
        label_counts[lbl if lbl in label_counts else 'other'] += 1
        if lbl == 'pending':
            continue  # skip unlabeled
        w = d['window'].astype(np.float32)
        if w.shape != (3, 100):
            continue
        windows.append(w)
        # positive=1, negative/noise=0
        labels.append(1 if lbl == 'positive' else 0)
        metas.append({'station': str(d['station']), 'unix_ts': float(d['unix_ts']),
                      'conf': float(d['conf']), 'label': lbl})
    except Exception as e:
        print(f'  skip {os.path.basename(fpath)}: {e}')

print(f'\nLabel counts:')
for k, v in label_counts.items():
    print(f'  {k:12s}: {v}')

n_labeled = len(windows)
n_pos = sum(labels)
n_neg = n_labeled - n_pos
print(f'\nUsable: {n_labeled} ({n_pos} positive, {n_neg} negative/noise)')

if n_labeled < args.min_samples:
    print(f'ERROR: only {n_labeled} labeled samples (need {args.min_samples}). '
          f'Run the sensor longer to collect more data.')
    sys.exit(1)

if args.dry_run:
    print('\n--dry-run: stopping here.')
    sys.exit(0)

# ── Train/eval split (stratified) ───────────────────────────────────────────
import random
pos_idxs = [i for i, l in enumerate(labels) if l == 1]
neg_idxs = [i for i, l in enumerate(labels) if l == 0]
random.shuffle(pos_idxs)
random.shuffle(neg_idxs)
split_p = max(1, int(len(pos_idxs) * args.eval_split))
split_n = max(1, int(len(neg_idxs) * args.eval_split))
eval_idxs  = pos_idxs[:split_p] + neg_idxs[:split_n]
train_idxs = pos_idxs[split_p:] + neg_idxs[split_n:]
print(f'  eval:  {split_p} positive + {split_n} negative/noise = {len(eval_idxs)}')
print(f'  train: {len(pos_idxs)-split_p} positive + {len(neg_idxs)-split_n} negative/noise = {len(train_idxs)}')

X_train = np.array([windows[i] for i in train_idxs])
y_train = np.array([labels[i]  for i in train_idxs], dtype=np.int64)
X_eval  = np.array([windows[i] for i in eval_idxs])
y_eval  = np.array([labels[i]  for i in eval_idxs],  dtype=np.int64)

print(f'\nTrain: {len(X_train)}  Eval: {len(X_eval)}')

# ── Class-balanced sampler ────────────────────────────────────────────────────
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import TensorDataset, DataLoader, WeightedRandomSampler
except ImportError:
    print('ERROR: torch not installed. Run: pip install torch --index-url https://download.pytorch.org/whl/cpu')
    sys.exit(1)

sys.path.insert(0, os.path.dirname(__file__))
from seismic.model import StreamingNet
from seismic.config import N_SEEDS, BUF_DECAY, BUF_STRENGTH, CYCLES

Xt = torch.tensor(X_train)
yt = torch.tensor(y_train)
Xe = torch.tensor(X_eval)
ye = torch.tensor(y_eval)

# Weighted sampler to handle class imbalance. Full inverse-frequency (power=1.0)
# trains on an artificial ~50/50 batch distribution, which pushes the model's
# implicit prior toward "positive is common" — that mismatches the real,
# heavily-skewed deployment distribution and shows up as high recall but
# collapsed precision. A softer power partially compensates without fully
# flattening it.
class_counts = np.bincount(y_train, minlength=2).astype(float)
class_counts = np.maximum(class_counts, 1)
weights = 1.0 / (class_counts ** args.sample_power)
sample_weights = torch.tensor([weights[y] for y in y_train], dtype=torch.float)
sampler = WeightedRandomSampler(sample_weights, num_samples=len(sample_weights), replacement=True)
train_ds = TensorDataset(Xt, yt)
train_dl = DataLoader(train_ds, batch_size=args.batch, sampler=sampler)
eval_ds  = TensorDataset(Xe, ye)
eval_dl  = DataLoader(eval_ds, batch_size=64)

n_seeds = min(args.seeds, N_SEEDS)
all_results = []

for seed in range(n_seeds):
    ckpt_path = os.path.join(args.checkpoints, f'seed_{seed}.pt')
    backup_path = os.path.join(args.checkpoints, f'seed_{seed}_pretrain.pt')

    print(f'\n--- Seed {seed} ---')
    if not os.path.exists(ckpt_path):
        print(f'  WARNING: {ckpt_path} not found — training from scratch')
        model = StreamingNet(perm_seed=seed)
    else:
        if not args.no_backup:
            shutil.copy2(ckpt_path, backup_path)
            print(f'  Backed up → {backup_path}')
        model = StreamingNet(perm_seed=seed)
        model.load_state_dict(torch.load(ckpt_path, map_location='cpu'))
        print(f'  Loaded pretrained weights')

    # Phase 1 (first half): freeze all but classifier head, higher LR
    # Phase 2 (second half): unfreeze all, lower LR for fine-tuning
    for p in model.parameters():
        p.requires_grad = False
    for p in model.cls.parameters():
        p.requires_grad = True
    for p in model.mag.parameters():
        p.requires_grad = True
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr)
    criterion = nn.CrossEntropyLoss()

    for epoch in range(args.epochs):
        if epoch == args.epochs // 2:
            # Unfreeze all layers, drop LR for careful fine-tuning
            for p in model.parameters():
                p.requires_grad = True
            optimizer = optim.Adam(model.parameters(), lr=args.lr * 0.1)

        model.train()
        total_loss, n_correct, n_total = 0.0, 0, 0
        for xb, yb in train_dl:
            optimizer.zero_grad()
            logits, _ = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * len(yb)
            n_correct += (logits.argmax(1) == yb).sum().item()
            n_total += len(yb)

        if (epoch + 1) % 5 == 0 or epoch == args.epochs - 1:
            model.eval()
            tp = fp = fn = tn = 0
            tp_t = fp_t = fn_t = tn_t = 0
            with torch.no_grad():
                for xb, yb in eval_dl:
                    logits = model(xb)[0]
                    preds = logits.argmax(1)
                    tp += ((preds == 1) & (yb == 1)).sum().item()
                    fp += ((preds == 1) & (yb == 0)).sum().item()
                    fn += ((preds == 0) & (yb == 1)).sum().item()
                    tn += ((preds == 0) & (yb == 0)).sum().item()

                    prob = torch.softmax(logits, dim=1)[:, 1]
                    preds_t = (prob >= args.eval_threshold).long()
                    tp_t += ((preds_t == 1) & (yb == 1)).sum().item()
                    fp_t += ((preds_t == 1) & (yb == 0)).sum().item()
                    fn_t += ((preds_t == 0) & (yb == 1)).sum().item()
                    tn_t += ((preds_t == 0) & (yb == 0)).sum().item()
            prec = tp / max(1, tp + fp)
            rec  = tp / max(1, tp + fn)
            f1   = 2 * prec * rec / max(1e-9, prec + rec)
            prec_t = tp_t / max(1, tp_t + fp_t)
            rec_t  = tp_t / max(1, tp_t + fn_t)
            f1_t   = 2 * prec_t * rec_t / max(1e-9, prec_t + rec_t)
            train_acc = n_correct / max(1, n_total)
            print(f'  epoch {epoch+1:3d}  loss={total_loss/n_total:.4f}  '
                  f'train_acc={train_acc:.3f}  '
                  f'[argmax] prec={prec:.3f} rec={rec:.3f} F1={f1:.3f}  '
                  f'(tp={tp} fp={fp} fn={fn} tn={tn})  |  '
                  f'[@{args.eval_threshold:.3f}] prec={prec_t:.3f} rec={rec_t:.3f} F1={f1_t:.3f}  '
                  f'(tp={tp_t} fp={fp_t} fn={fn_t} tn={tn_t})')

    torch.save(model.state_dict(), ckpt_path)
    print(f'  Saved → {ckpt_path}')
    all_results.append({'seed': seed, 'prec': prec_t, 'rec': rec_t, 'f1': f1_t,
                         'prec_argmax': prec, 'rec_argmax': rec, 'f1_argmax': f1})

print(f'\n=== Summary (metrics @ eval-threshold={args.eval_threshold:.3f}) ===')
for r in all_results:
    print(f"  seed {r['seed']}: prec={r['prec']:.3f}  rec={r['rec']:.3f}  F1={r['f1']:.3f}  "
          f"(argmax: prec={r['prec_argmax']:.3f} rec={r['rec_argmax']:.3f} F1={r['f1_argmax']:.3f})")
avg_f1 = np.mean([r['f1'] for r in all_results])
print(f"\n  Mean F1: {avg_f1:.3f}")
if avg_f1 < 0.7:
    print('  WARNING: F1 < 0.7 — consider collecting more data or tuning hyperparams')
    print('  Use --dry-run to inspect dataset stats before training.')
print('\nDone. Restart the sensor to load new checkpoints.')
