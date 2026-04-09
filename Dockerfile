# ============================================================
# Brand Guardian AI — Dockerfile (ECS-ready, multi-stage)
# ============================================================
#
# Stage 1: Builder — installs dependencies in isolation
# Stage 2: Runtime — minimal image, no dev tools
#
# ECS deployment notes:
#   • YOUTUBE_COOKIES_B64: Store Netscape cookies.txt in AWS Secrets Manager
#     as a Base64-encoded string. The entrypoint decodes it at startup.
#   • YOUTUBE_LIMIT_RATE: Throttle yt-dlp download speed (e.g. '5M').
#   • YOUTUBE_USER_AGENT: Match the browser UA used when generating cookies.
#   • YOUTUBE_SLEEP_INTERVAL / YOUTUBE_MAX_SLEEP_INTERVAL: Randomize delays.
#   • REDIS_URL: ElastiCache endpoint for shared rate limiting across replicas.
#   • HEALTHCHECK maps to the ECS container health check.
#
# Bot-detection mitigation (AWS datacenter IPs are often flagged by YouTube):
#   • Always provide fresh cookies exported from a real browser session.
#   • Update yt-dlp nightly (handled by the pip install -U step below).
#   • Throttle speed + randomize sleep intervals to mimic organic traffic.
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

# Always upgrade yt-dlp to the latest release at build time.
# yt-dlp releases bot-detection fixes almost daily — staying current is critical.
RUN pip install --no-cache-dir --prefix=/install -U yt-dlp

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

# Copy entrypoint script and ensure it is executable
COPY entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh
# Ensure directories have proper permissions if appuser needs to write locally
RUN chown -R 1000:1000 /app

# Non-root user for security best-practices on ECS
USER 1000

# ECS HEALTHCHECK — maps directly to the ECS "container health check" setting
HEALTHCHECK --interval=20s --timeout=15s --start-period=90s --retries=5 \
    CMD curl -f http://127.0.0.1:${PORT}/api/health || exit 1

EXPOSE ${PORT}

# Startup via the custom entrypoint script (decodes Base64 secrets)
ENTRYPOINT ["/app/entrypoint.sh"]

# For ECS: override CMD in the Task Definition to run the worker instead:
#   CMD ["python", "-m", "ComplianceQAPipeline.backend.src.worker"]
CMD ["sh", "-c", "uvicorn ComplianceQAPipeline.backend.src.api.server:app \
    --host 0.0.0.0 \
    --port ${PORT} \
    --access-log"]
