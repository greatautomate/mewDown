FROM python:3.11-slim

WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    wget \
    curl \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r botuser && useradd -r -g botuser botuser

# Create directories
RUN mkdir -p /tmp/videos /app/sessions \
    && chown -R botuser:botuser /tmp/videos /app/sessions

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .

# Set ownership
RUN chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Verify FFmpeg
RUN ffmpeg -version

# Run bot
CMD ["python", "bot.py"]
