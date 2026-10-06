FROM python:3.12-slim

WORKDIR /app

# System deps for psycopg (PostgreSQL client) and Playwright (Chromium)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer cached unless pyproject.toml changes)
COPY pyproject.toml requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

# Install the project package
COPY . .
RUN pip install --no-cache-dir --no-deps -e .

# Install Chromium browser for Playwright (required for screenshot rendering)
RUN playwright install chromium --with-deps

# Create data directory and drop to a non-root user for security
RUN mkdir -p /app/data && useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser /app/data
USER appuser

# Health check — confirm the worker process is alive every 30 seconds
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f "rank-agent" > /dev/null || exit 1

CMD ["rank-agent", "worker"]
