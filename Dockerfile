# ============================================================
# Multi-stage Dockerfile for ops-agent
# Stage 1 (builder): install all Python deps into a venv
# Stage 2 (runtime): slim image, non-root user, healthcheck
# ============================================================

# ---- Stage 1: builder ----
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only dependency files first for better layer caching
COPY requirements.txt pyproject.toml ./

# Create a venv and install into it
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir structlog qdrant-client "psycopg[binary]" \
       sqlalchemy alembic python-dotenv pydantic-settings


# ---- Stage 2: runtime ----
FROM python:3.12-slim AS runtime

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -s /bin/bash -u 1000 agent

WORKDIR /app

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY --chown=agent:agent . .

# Ensure case directories exist with correct permissions
RUN mkdir -p /app/cases/pending /app/cases/exported \
    && chown -R agent:agent /app/cases

USER agent

EXPOSE 8000

# Healthcheck calls the /health endpoint added in server.py
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.web.server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--timeout-keep-alive", "30"]
