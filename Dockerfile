FROM python:3.11-slim

WORKDIR /app

# System deps for ObsPy
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch CPU-only first (separate index), then remaining deps — one cached layer
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir obspy numpy scipy flask rich

COPY sensor.py .

# Checkpoints baked in (~900KB total) for Fly.io deploy.
# For local docker-compose, the volume mount overrides this directory.
RUN mkdir -p /checkpoints
COPY checkpoints/ /checkpoints/

ENV CHECKPOINT_DIR=/checkpoints \
    SEEDLINK_SERVER=liss.usgs.gov:4000 \
    NETWORK=IU \
    STATION=MAJO \
    CHANNELS=HHZ,HHN,HHE \
    THRESHOLD=0.835 \
    N_SEEDS=3

CMD ["python", "sensor.py"]
