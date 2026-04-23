# AI Agents Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies including git for pip to install from GitHub
RUN apt-get update && apt-get install -y \
    gcc \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install SignalWire SDK from local copy (repo is private, can't clone in Docker)
COPY signalwire-sdk /signalwire-sdk
RUN pip install --no-cache-dir /signalwire-sdk

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8080

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Default command
CMD ["python", "main_agent.py"]