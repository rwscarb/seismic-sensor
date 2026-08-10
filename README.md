# seismic-sensor

Real-time seismic P-wave detector using a streaming neural network ensemble, deployed to [seismic.fib896.com](https://seismic.fib896.com).

## How it works

A 3-seed ensemble of **StreamingNet** models (1D CNN + orbit-permuted Hebbian buffer) listens to live SeedLink feeds from GEOFON and IRIS. When N_CONSENSUS stations independently fire within a 120-second window, a detection is logged. Epicenters are estimated via a flat-earth P-wave arrival time inversion (Nelder-Mead). Detections are cross-checked against USGS and EMSC earthquake catalogs.

```
SeedLink stream → normalize → StreamingNet × 3 seeds → ensemble vote
→ consensus across stations → epicenter localization → catalog lookup → dashboard
```

## Architecture

**StreamingNet** — 3-channel 1D CNN with an orbit-permuted Hebbian buffer:

```
Input: (3, 100)  — 3-component seismogram, 1 second at 100 Hz
→ ConvBlock(3→32) → ConvBlock(32→64) → ConvBlock(64→128) → AdaptiveAvgPool
→ orbit-permuted Hebbian buffer (CYCLES=1, DECAY=0.876, STRENGTH=1.429)
→ Linear(128, 2)  — seismic / noise classifier
→ Linear(128, 1)  — magnitude estimator
```

The buffer permutation is seeded per ensemble member, diversifying the feature basis across seeds. Trained on the [STEAD dataset](https://github.com/smousavi05/STEAD) (chunk2) with magnitude-stratified sampling.

**Performance (STEAD holdout, threshold=0.835):** 88.0% precision / 95.7% recall

## Stations

| Network | Code | Location |
|---------|------|----------|
| GE | APE | Aegean, Greece |
| GE | MORC | Morava, Czech Republic |
| GE | KBS | Svalbard, Norway |
| GE | WLF | Walferdange, Luxembourg |
| GE | MATE | Matera, Italy |
| GE | KARP | Karpathos, Greece |
| IU | COR | Corvallis, Oregon |
| CN | PGC | Saanich, British Columbia |
| IU | KDAK | Kodiak Island, Alaska |
| IU | COLA | College, Alaska |

GEOFON stations stream via `geofon.gfz-potsdam.de:18000`. IRIS/CWBR stations stream via `rtserve.iris.washington.edu:18000`.

## Deploy

```bash
# Deploy to Fly.io (requires fly CLI + authenticated account)
make deploy

# Force full rebuild (e.g. after Dockerfile changes)
make deploy-clean

# Tail live logs
make fly-logs
```

## Train new seeds

Requires [STEAD chunk2](https://zenodo.org/records/3911667):

```bash
wget -c 'https://zenodo.org/records/3911667/files/chunk2.hdf5'
wget -c 'https://zenodo.org/records/3911667/files/chunk2.csv'

pip install torch numpy h5py scipy scikit-learn tqdm pandas

python train.py --seeds 3 --epochs 30 --out checkpoints/
# then redeploy to pick up new weights:
make deploy-clean
```

GPU strongly recommended (RTX 3070 or better). CPU training for 3 seeds × 30 epochs takes several hours.

## Local development

```bash
cp .env.example .env
# edit .env — set SEEDLINK_SERVER, STATIONS, etc.
make dev       # starts via docker compose
make logs      # tail logs
make shell     # bash into container
```

## Configuration

All parameters are set via environment variables (see `fly.toml` and `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `SEEDLINK_SERVER` | `geofon.gfz-potsdam.de:18000` | Primary SeedLink server |
| `IRIS_SERVER` | `rtserve.iris.washington.edu:18000` | Secondary SeedLink server |
| `STATIONS` | `GE.APE,...` | Comma-separated station list for primary server |
| `IRIS_STATIONS` | `IU.COR,...` | Comma-separated station list for secondary server |
| `CHANNELS` | `HHZ,HHN,HHE` | Seismic channels |
| `THRESHOLD` | `0.835` | Detection confidence threshold |
| `N_CONSENSUS` | `3` | Stations required to confirm a detection |
| `CONSENSUS_WINDOW` | `120` | Seconds within which consensus must occur |
| `N_SEEDS` | `3` | Ensemble size |

## License

MIT — see [LICENSE](LICENSE)
