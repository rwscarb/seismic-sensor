"""
train.py — Train StreamingNet seeds for the seismic sensor ensemble.

Usage:
  python train.py [--seeds 3] [--epochs 30] [--out checkpoints/]

Requirements:
  pip install torch numpy h5py scipy scikit-learn tqdm
  (CPU training works; GPU is much faster — set CUDA_VISIBLE_DEVICES if available)

Dataset: STEAD (STanford EArthquake Dataset) — chunk2 HDF5
  Download from: https://github.com/smousavi05/STEAD
  Or:  wget -c "https://zenodo.org/records/3911667/files/chunk2.hdf5"
       wget -c "https://zenodo.org/records/3911667/files/chunk2.csv"

Place chunk2.hdf5 and chunk2.csv in the current directory (or set --data).
"""

import argparse, os, random, sys
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from pathlib import Path

# ── Hyperparameters — must match sensor.py exactly ──────────────────────────
K            = 128
CYCLES       = 1
WIN_SAMPLES  = 100
BUF_DECAY    = 0.876
BUF_STRENGTH = 1.429
SAMPLE_RATE  = 100   # Hz — STEAD waveforms

# Training defaults
LR           = 1e-3
EPOCHS       = 30
BATCH        = 64
PER_BIN      = 2666   # events per magnitude bin (balances class distribution)
NOISE_STD    = 0.3    # augmentation noise σ
THRESHOLD    = 0.835  # decision threshold for evaluation (must match fly.toml)


# ── Architecture (mirrors sensor.py) ────────────────────────────────────────
class ConvBlock(nn.Module):
    def __init__(self, ci, co, k=7):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(ci, co, k, padding=k // 2),
            nn.BatchNorm1d(co),
            nn.ReLU(),
        )

    def forward(self, x):
        return self.net(x)


class StreamingNet(nn.Module):
    def __init__(self, perm_seed=0):
        super().__init__()
        self.enc = nn.Sequential(
            ConvBlock(3, 32),
            ConvBlock(32, 64),
            ConvBlock(64, K),
            nn.AdaptiveAvgPool1d(1),
        )
        perm = torch.tensor(
            np.random.RandomState(perm_seed).permutation(K), dtype=torch.long
        )
        self.register_buffer("perm", perm)
        self.cls = nn.Linear(K, 2)
        self.mag = nn.Linear(K, 1)

    def forward(self, x):
        h = self.enc(x).squeeze(-1)
        buf = torch.zeros_like(h)
        for _ in range(CYCLES):
            h = torch.relu(h[:, self.perm])
            buf = BUF_DECAY * buf + (1 - BUF_DECAY) * h.detach()
            h = h + BUF_STRENGTH * buf
        return self.cls(h), self.mag(h).squeeze(-1)


# ── Dataset ──────────────────────────────────────────────────────────────────
class SteadDataset(Dataset):
    """
    STEAD chunk2 dataset.  Each item is (waveform, label, magnitude).
    waveform: (3, WIN_SAMPLES) float32, per-channel z-scored
    label:    0 = noise, 1 = earthquake
    magnitude: float (0.0 for noise)
    """

    def __init__(self, hdf5_path, csv_path, augment=False):
        import h5py, pandas as pd

        self.augment = augment
        self.f = h5py.File(hdf5_path, "r")
        self.waveforms = self.f["data"]
        df = pd.read_csv(csv_path)

        # Split into earthquakes and noise
        eq = df[df["trace_category"] == "earthquake_local"].copy()
        ns = df[df["trace_category"] == "noise"].copy()

        # Magnitude-stratified sampling for earthquakes
        eq["mag_bin"] = pd.cut(
            eq["source_magnitude"].fillna(0),
            bins=[-99, 3, 5, 99],
            labels=["lt3", "m3_5", "gt5"],
        )
        eq_sampled = (
            eq.groupby("mag_bin", observed=True)
            .apply(lambda g: g.sample(min(len(g), PER_BIN), random_state=42))
            .reset_index(drop=True)
        )

        # Balance noise against total earthquakes
        n_noise = len(eq_sampled)
        ns_sampled = ns.sample(min(len(ns), n_noise), random_state=42)

        eq_sampled["label"] = 1
        ns_sampled["label"] = 0
        ns_sampled["source_magnitude"] = 0.0

        combined = pd.concat(
            [eq_sampled[["trace_name", "label", "source_magnitude"]],
             ns_sampled[["trace_name", "label", "source_magnitude"]]],
            ignore_index=True,
        ).sample(frac=1, random_state=42).reset_index(drop=True)

        self.names = combined["trace_name"].tolist()
        self.labels = combined["label"].tolist()
        self.mags = combined["source_magnitude"].fillna(0).tolist()

        # Per-label weights for WeightedRandomSampler
        counts = {0: combined["label"].eq(0).sum(), 1: combined["label"].eq(1).sum()}
        self.sample_weights = [1.0 / counts[l] for l in self.labels]

    def __len__(self):
        return len(self.names)

    def _load(self, idx):
        raw = self.waveforms[self.names[idx]][:, :WIN_SAMPLES * SAMPLE_RATE // SAMPLE_RATE]
        # raw shape: (3, full_length) — take first WIN_SAMPLES
        w = raw[:, :WIN_SAMPLES].astype(np.float32)
        return w

    @staticmethod
    def _normalise(w):
        for i in range(3):
            s = w[i].std()
            if s > 1e-6:
                w[i] /= s
        return w

    def __getitem__(self, idx):
        w = self._load(idx)
        w = self._normalise(w)
        if self.augment:
            w += np.random.randn(*w.shape).astype(np.float32) * NOISE_STD
        label = self.labels[idx]
        mag = float(self.mags[idx])
        return torch.tensor(w), torch.tensor(label, dtype=torch.long), torch.tensor(mag, dtype=torch.float32)


# ── Training loop ─────────────────────────────────────────────────────────────
def train_seed(seed, train_ds, val_ds, epochs, device, out_dir):
    print(f"\n{'='*60}\n  Training seed {seed}\n{'='*60}")
    torch.manual_seed(seed * 1000 + 42)
    np.random.seed(seed * 1000 + 42)

    model = StreamingNet(perm_seed=seed).to(device)

    sampler = WeightedRandomSampler(train_ds.sample_weights, len(train_ds), replacement=True)
    train_loader = DataLoader(train_ds, batch_size=BATCH, sampler=sampler, num_workers=2, pin_memory=True)
    val_loader   = DataLoader(val_ds,   batch_size=BATCH, shuffle=False,  num_workers=2, pin_memory=True)

    opt = torch.optim.Adam(model.parameters(), lr=LR)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    best_f1, best_state = 0.0, None

    for epoch in range(1, epochs + 1):
        # ── train ──────────────────────────────────────────────────────────
        model.train()
        total_loss = 0.0
        for xb, yb, mb in train_loader:
            xb, yb, mb = xb.to(device), yb.to(device), mb.to(device)
            logits, mag_pred = model(xb)
            loss_cls = F.cross_entropy(logits, yb)
            eq_mask = (yb == 1)
            loss_mag = F.mse_loss(mag_pred[eq_mask], mb[eq_mask]) if eq_mask.any() else 0.0
            loss = loss_cls + 0.1 * loss_mag
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += float(loss_cls)
        sched.step()

        # ── validate ────────────────────────────────────────────────────────
        model.eval()
        tp = fp = fn = tn = 0
        with torch.no_grad():
            for xb, yb, _ in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                logits, _ = model(xb)
                probs = F.softmax(logits / 1.0, dim=1)[:, 1]
                preds = (probs >= THRESHOLD).long()
                tp += ((preds == 1) & (yb == 1)).sum().item()
                fp += ((preds == 1) & (yb == 0)).sum().item()
                fn += ((preds == 0) & (yb == 1)).sum().item()
                tn += ((preds == 0) & (yb == 0)).sum().item()

        prec = tp / (tp + fp + 1e-9)
        rec  = tp / (tp + fn + 1e-9)
        f1   = 2 * prec * rec / (prec + rec + 1e-9)
        print(f"  epoch {epoch:3d}/{epochs}  loss={total_loss/len(train_loader):.4f}  "
              f"prec={prec:.3f}  rec={rec:.3f}  f1={f1:.3f}")

        if f1 > best_f1:
            best_f1 = f1
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

    out_path = os.path.join(out_dir, f"seed_{seed}.pt")
    torch.save(best_state, out_path)
    print(f"  → saved {out_path}  (best val f1={best_f1:.3f})")
    return best_f1


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Train StreamingNet seeds")
    ap.add_argument("--data",   default=".", help="Directory containing chunk2.hdf5 and chunk2.csv")
    ap.add_argument("--out",    default="checkpoints", help="Output directory for .pt files")
    ap.add_argument("--seeds",  type=int, default=3, help="Number of seeds to train")
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    ap.add_argument("--val-split", type=float, default=0.15, help="Fraction of data held out for validation")
    args = ap.parse_args()

    hdf5 = os.path.join(args.data, "chunk2.hdf5")
    csv  = os.path.join(args.data, "chunk2.csv")
    if not os.path.exists(hdf5):
        print(f"ERROR: {hdf5} not found.\n"
              "Download STEAD chunk2:\n"
              "  wget -c 'https://zenodo.org/records/3911667/files/chunk2.hdf5'\n"
              "  wget -c 'https://zenodo.org/records/3911667/files/chunk2.csv'",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # Build full dataset, then split into train/val
    full_ds = SteadDataset(hdf5, csv, augment=False)
    n_val   = int(len(full_ds) * args.val_split)
    n_train = len(full_ds) - n_val
    idx     = list(range(len(full_ds)))
    random.shuffle(idx)
    train_idx, val_idx = idx[:n_train], idx[n_train:]

    # Wrap subsets (augmentation only on train)
    from torch.utils.data import Subset
    train_sub = Subset(full_ds, train_idx)
    val_sub   = Subset(full_ds, val_idx)

    # Enable augmentation for training subset via a wrapper
    class AugmentedSubset(Dataset):
        def __init__(self, subset):
            self.subset = subset
        def __len__(self):
            return len(self.subset)
        def __getitem__(self, i):
            w, y, m = self.subset[i]
            w = w + torch.randn_like(w) * NOISE_STD
            return w, y, m
        @property
        def sample_weights(self):
            return [full_ds.sample_weights[j] for j in train_idx]

    train_ds = AugmentedSubset(train_sub)
    val_ds   = val_sub

    results = {}
    for seed in range(args.seeds):
        f1 = train_seed(seed, train_ds, val_ds, args.epochs, device, args.out)
        results[seed] = f1

    print("\n── Summary ──────────────────────────────────────────────────")
    for seed, f1 in results.items():
        print(f"  seed {seed}: best val f1 = {f1:.3f}")
    print(f"\nCheckpoints written to: {args.out}/")
    print("Deploy: make deploy-clean  (to pick up new .pt files)")


if __name__ == "__main__":
    main()
