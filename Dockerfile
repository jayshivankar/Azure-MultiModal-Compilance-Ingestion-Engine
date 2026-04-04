# ============================================================
# Brand Guardian AI — Dockerfile (ECS-ready, multi-stage)
# ============================================================
#
# Stage 1: Builder — installs dependencies in isolation
# Stage 2: Runtime — minimal image, no dev tools
#
# ECS deployment notes:
#   • Set YOUTUBE_COOKIES_FILE via ECS Task Definition secret
#     (store cookies.txt in AWS Secrets Manager, mount as a volume)
#   • Set REDIS_URL to your ElastiCache endpoint for shared rate limiting
#   • HEALTHCHECK maps to the ECS container health check
# ============================================================

# ── Stage 1: Builder ─────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build-time system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only pyproject.toml first (leverage Docker layer cache)
COPY pyproject.toml ./
COPY README.md ./

# Install the project dependencies into a prefix dir
RUN pip install --no-cache-dir --prefix=/install .

# ── Stage 2: Runtime ─────────────────────────────────────────
FROM python:3.12-slim AS runtime

# ECS container best-practices
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    # Prevents Python from creating __pycache__ dirs in the container
    PYTHONPYCACHEPREFIX=/tmp/__pycache__

WORKDIR /app

# System runtime dependencies (ffmpeg required by yt-dlp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY . /app/

# ECS HEALTHCHECK — maps directly to the ECS "container health check" setting
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

# Non-root user for security best-practices on ECS
RUN adduser --system --no-create-home --uid 1000 appuser
USER appuser

EXPOSE ${PORT}

# Startup command
# For ECS: override CMD in the Task Definition to run the worker instead:
#   CMD ["python", "-m", "ComplianceQAPipeline.backend.src.worker"]
CMD ["sh", "-c", "uvicorn ComplianceQAPipeline.backend.src.api.server:app \
    --host 0.0.0.0 \
    --port ${PORT} \
    --workers 2 \
    --loop uvloop \
    --access-log"]
