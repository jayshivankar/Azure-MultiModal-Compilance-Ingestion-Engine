#!/bin/sh
set -e

# ---------------------------------------------------------
# AWS ECS / Docker Entrypoint
# ---------------------------------------------------------
# Many platforms (ECS, Railway, Render) make it difficult
# to mount text files securely. The industry standard is to
# inject the file contents as a Base64-encoded environment
# variable.
#
# If the YOUTUBE_COOKIES_B64 environment variable is present,
# decode it and write it to disk so yt-dlp can use it.
# ---------------------------------------------------------

if [ -n "$YOUTUBE_COOKIES_B64" ]; then
    echo "[Entrypoint] Detected YOUTUBE_COOKIES_B64. Decoding to secure file..."
    # Ensure directory exists just in case
    mkdir -p /app/ComplianceQAPipeline
    
    # Needs to be decoded without assuming GNU base64 format limits
    echo "Decoding Base64 cookies into /app/ComplianceQAPipeline/cookies.txt setup..."
    echo "$YOUTUBE_COOKIES_B64" | base64 -d > /app/ComplianceQAPipeline/cookies.txt
    
    # Export the environment variable the python app expects
    export YOUTUBE_COOKIES_FILE=/app/ComplianceQAPipeline/cookies.txt
elif [ -n "$YOUTUBE_COOKIES_FILE" ]; then
    echo "[Entrypoint] YOUTUBE_COOKIES_FILE is explicitly set to: $YOUTUBE_COOKIES_FILE"
fi

# Hand over process control to the CMD specified in the Dockerfile
exec "$@"
