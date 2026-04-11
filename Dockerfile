# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies into a prefix we can copy later
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime system libraries required by PyTorch / soundfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Pull installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY . .

# HuggingFace model cache — mount a volume here to persist downloaded models
ENV HF_HOME=/cache/huggingface
VOLUME ["/cache"]

# Runtime directories are created by the app, but pre-create them so the
# container user has correct ownership even before first request.
RUN mkdir -p tmp sessions

# Environment variable defaults (all overridable at runtime)
ENV OLLAMA_CHAT_URL=http://ollama:11434/api/chat \
    AEGIS_MODE=semi \
    KOKORO_VOICE=af_heart \
    AGENT_MAX_ITERS=5 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
