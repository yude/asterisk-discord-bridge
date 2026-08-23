FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    libopus0 \
    libsodium-dev \
    libffi-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen || uv sync

COPY src/main.py src/audio_bridge.py ./

ENV PYTHONUNBUFFERED=1

CMD ["uv", "run", "main.py"]
