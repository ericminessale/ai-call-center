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
# 3.0.2 declares `setuptools<81,>=66.1.1`, which caps setuptools below the
# fix. Verified in the built image: setuptools 79.0.1 — the version
# python:3.11-slim already ships, which satisfies the cap, so pip has no
# reason to touch it either way. (Two earlier revisions of this file got the
# mechanics wrong: one upgraded setuptools here and claimed that fixed it, the
# next said the requirements install downgrades it back to 80.10.2. Neither
# happens — the base version simply stands.) Forcing >=83.0.0 would break the
# SDK's declared constraint for a flaw that cannot affect this image:
#
#   The bug is in `FileList`/`MANIFEST.in` handling while BUILDING an sdist —
#   NFD/NFC filename mismatches on macOS APFS/HFS+ let an excluded file get
#   packed into a .tar.gz. It affects package maintainers publishing sdists on
#   macOS. This is a Linux runtime image that never invokes `setup.py sdist`.
#
# Revisit when signalwire-sdk lifts the <81 cap. pip-audit reports it as two
# findings; it is one advisory, double-sourced (PyPI + OSV) under one ID.
# `pip-audit` run INSIDE the built image reports this and nothing else.
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