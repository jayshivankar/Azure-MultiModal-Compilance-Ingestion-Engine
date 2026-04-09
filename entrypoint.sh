#!/bin/sh
set -e

# ---------------------------------------------------------
# AWS ECS / Docker Entrypoint — Brand Guardian AI
# ---------------------------------------------------------
# Cookie injection strategy for ECS environments:
#
#   Option 1 — YOUTUBE_COOKIES_B64 (preferred for AWS Secrets Manager):
#     Store the full Netscape cookies.txt content as a Base64-encoded
#     string in AWS Secrets Manager. Inject it as an environment variable
#     in your ECS Task Definition. This entrypoint decodes it at startup.
#
#   Option 2 — YOUTUBE_COOKIES_FILE (for volume mounts / local dev):
#     Set this to the absolute path of a cookies.txt file already present
#     in the container (e.g. mounted via EFS or a sidecar).
#
# Security: The decoded file is written to /tmp (ephemeral container
# storage) so it never persists between task runs.
# ---------------------------------------------------------

if [ -n "$YOUTUBE_COOKIES_B64" ]; then
    echo "[Entrypoint] Detected YOUTUBE_COOKIES_B64 — decoding to /tmp/cookies.txt ..."

    # Decode without GNU base64 line-length constraints (POSIX-safe).
    printf '%s' "$YOUTUBE_COOKIES_B64" | base64 -d > /tmp/cookies.txt

    # Restrict access: only the app user should be able to read this.
    chmod 600 /tmp/cookies.txt

    echo "[Entrypoint] cookies.txt written to /tmp/cookies.txt ($(wc -l < /tmp/cookies.txt) lines)."

    # Override the env var so the Python app picks up the decoded path.
    export YOUTUBE_COOKIES_FILE=/tmp/cookies.txt

elif [ -n "$YOUTUBE_COOKIES_FILE" ]; then
    if [ -f "$YOUTUBE_COOKIES_FILE" ]; then
        echo "[Entrypoint] YOUTUBE_COOKIES_FILE set to: $YOUTUBE_COOKIES_FILE"
        chmod 600 "$YOUTUBE_COOKIES_FILE" 2>/dev/null || true
    else
        echo "[Entrypoint] WARNING: YOUTUBE_COOKIES_FILE='$YOUTUBE_COOKIES_FILE' does not exist!" >&2
        echo "[Entrypoint] yt-dlp will run WITHOUT cookies — bot detection risk is HIGH on cloud IPs." >&2
    fi

else
    echo "[Entrypoint] WARNING: Neither YOUTUBE_COOKIES_B64 nor YOUTUBE_COOKIES_FILE is set." >&2
    echo "[Entrypoint] yt-dlp will run WITHOUT cookies — expect bot detection failures on AWS." >&2
fi

# Hand over process control to the CMD from the Dockerfile (or Task Definition override).
exec "$@"
