FROM python:3.10.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libgomp1 \
    git \
    ffmpeg \
    gcc \
    g++ \
    python3-dev \
    build-essential && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Upgrade pip and install wheel
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip setuptools wheel

# Install uvicorn with websocket support
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install 'uvicorn[standard]'

# Install common dependencies that bolna requires
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
    python-dotenv \
    pydantic \
    fastapi \
    huggingface-hub \
    numpy \
    tqdm \
    requests \
    twilio

# Copy the local bolna package source from repo root
COPY bolna /app/bolna_src/bolna
COPY pyproject.toml /app/bolna_src/pyproject.toml
COPY requirements.txt /app/bolna_src/requirements.txt
COPY README.md /app/bolna_src/README.md

# Install bolna from local source
RUN --mount=type=cache,target=/root/.cache/pip \
    cd /app/bolna_src && \
    pip install --verbose . || \
    (echo "Failed to install bolna package. See error above." && exit 1)

# Copy application files from local_setup
COPY local_setup/quickstart_server.py /app/
COPY local_setup/presets /app/presets

EXPOSE 5001

CMD ["uvicorn", "quickstart_server:app", "--host", "0.0.0.0", "--port", "5001"]
