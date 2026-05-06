# Multi-stage build for Distributed Chord Infrastructure
# Stage 1: Builder
FROM python:3.11-slim AS builder

WORKDIR /build

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

# Install runtime-only system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder — prefix=/install mirrors /usr/local layout
# so packages land at /usr/local/lib/python3.11/site-packages/ (always on sys.path)
COPY --from=builder /install /usr/local

# Copy application code
COPY chord/      ./chord/
COPY storage/    ./storage/
COPY api/        ./api/
COPY run_node.py .
COPY submit_job.py .
COPY entrypoint.sh .

# Make entrypoint executable
RUN chmod +x entrypoint.sh

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Create non-root user for security
RUN useradd -m -u 1000 chord && chown -R chord:chord /app
USER chord

# Health check
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD curl -sf http://localhost:${CHORD_PORT:-5000}/chord/ping || exit 1

EXPOSE 5000

ENTRYPOINT ["sh", "entrypoint.sh"]
