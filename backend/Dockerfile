# Flask Backend Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies.
#
# ACCEPTED ADVISORY — setuptools PYSEC-2026-3447 / CVE-2026-59890 (fixed in
# 83.0.0) is NOT cleared here and cannot be from this file: signalwire-sdk
# 3.0.2 declares `setuptools<81,>=66.1.1`, so pip resolves setuptools to
# 80.10.2 no matter what an earlier `--upgrade setuptools` installed. (An
# earlier revision of this Dockerfile upgraded setuptools on the line below
# and claimed that fixed it — it didn't; the requirements install downgraded
# it right back.) Forcing >=83.0.0 would break the SDK's declared constraint
# for a flaw that cannot affect this image:
#
#   The bug is in `FileList`/`MANIFEST.in` handling while BUILDING an sdist —
#   NFD/NFC filename mismatches on macOS APFS/HFS+ let an excluded file get
#   packed into a .tar.gz. It affects package maintainers publishing sdists on
#   macOS. This is a Linux runtime image that never invokes `setup.py sdist`.
#
# Revisit when signalwire-sdk lifts the <81 cap. pip-audit reports it as two
# findings; it is one advisory, double-sourced (PyPI + OSV) under one ID.
RUN pip install --no-cache-dir --upgrade pip wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 5000

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# Default command (overridden by docker-compose)
CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:5000", "--timeout", "120", "wsgi:app"]