# Dockerfile
# Single-stage build: this project has no JS build step (HTMX via CDN
# script tag, no npm/webpack/vite), so there is nothing to compile before
# the Python image runs. That keeps this Dockerfile intentionally simple -
# a multi-stage build exists to separate a heavy build toolchain from a
# lean runtime image, and this project never has a heavy build toolchain
# to begin with.

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (separate layer) so `docker compose up` after
# a code-only change reuses the cached dependency layer instead of
# reinstalling every package.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application.
COPY . .

# SQLite database directory; created here too so a bind-mounted volume
# (see docker-compose.yml) has somewhere to land even on first run.
RUN mkdir -p /app/data

EXPOSE 8000

# Liveness check hits the same /healthz route a human would use to confirm
# the app booted; keeps this Dockerfile self-documenting about how to
# verify the container is actually up, not just running.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
