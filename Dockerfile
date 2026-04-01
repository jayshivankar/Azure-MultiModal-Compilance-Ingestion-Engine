# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Install system dependencies
# ffmpeg is required by yt-dlp for video processing
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the project files into the container
COPY . /app/

# Install the project and its dependencies
# Using 'pip install .' to install dependencies from pyproject.toml
RUN pip install --no-cache-dir .

# Expose the port that Railway will use
EXPOSE ${PORT}

# Run the FastAPI server
# We use the $PORT environment variable assigned by Railway
CMD ["sh", "-c", "uvicorn ComplianceQAPipeline.backend.src.api.server:app --host 0.0.0.0 --port ${PORT}"]
