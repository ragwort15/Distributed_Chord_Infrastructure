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
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

# Install runtime-only system dependencies (gRPC, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local

# Copy application code
COPY chord/ ./chord/
COPY storage/ ./storage/
COPY api/ ./api/
COPY simulator/ ./simulator/
COPY run_node.py .
COPY submit_job.py .

# Set environment variables
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHORD_PORT=${CHORD_PORT:-5000} \
    CHORD_ID=${CHORD_ID:-1} \
    CHORD_JOIN=${CHORD_JOIN:-""}

# Create non-root user for security
RUN useradd -m -u 1000 chord && chown -R chord:chord /app
USER chord

# Health check
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${CHORD_PORT}/chord/ping || exit 1

# Expose default port
EXPOSE 5000

# Run the application
CMD ["python", "run_node.py"]
