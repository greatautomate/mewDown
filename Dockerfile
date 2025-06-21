# Use Python 3.11 slim image for smaller size
FROM python:3.11-slim

# Set metadata
LABEL maintainer="your-email@example.com"
LABEL description="ClassPlus Video Downloader Telegram Bot"
LABEL version="1.0.0"

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1    PYTHONDONTWRITEBYTECODE=1    DEBIAN_FRONTEND=noninteractive    PIP_NO_CACHE_DIR=1    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends    ffmpeg    wget    curl    ca-certificates    && apt-get clean    && rm -rf /var/lib/apt/lists/*    && rm -rf /tmp/*    && rm -rf /var/tmp/*

# Create non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser botuser

# Create necessary directories
RUN mkdir -p /tmp/videos /app/logs    && chown -R botuser:botuser /tmp/videos /app/logs

# Copy requirements first (for better Docker layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bot.py .
COPY docs/ ./docs/

# Change ownership of app directory
RUN chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Verify FFmpeg installation
RUN ffmpeg -version

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3    CMD python -c "import requests; requests.get('http://localhost:8080/health', timeout=5)" || exit 1

# Expose port for health check
EXPOSE 8080

# Run the bot
CMD ["python", "bot.py"]
