# Networthy — NSDL CAS net-worth tracker
# Multi-stage build: install deps into a venv, then copy into a slim runtime.

FROM python:3.11-slim AS builder

# pikepdf/pdfplumber ship manylinux wheels, so no build toolchain is needed.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Create an isolated virtualenv we can copy wholesale into the runtime image.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_PORT=8000

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Bring in the pre-built dependencies.
COPY --from=builder /opt/venv /opt/venv

# Application code.
COPY app ./app

# Parsed financial data (SQLite DB + working files) lives here; mount a volume
# to persist it across container restarts.
RUN mkdir -p /app/data && chown -R appuser:appuser /app
VOLUME ["/app/data"]

USER appuser

EXPOSE 8000

# Basic liveness check against the health route (honors APP_PORT).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('APP_PORT','8000')).status==200 else 1)"

# Bind to APP_PORT (expanded at runtime); exec so uvicorn is PID 1 and receives
# SIGTERM for graceful shutdown on `docker stop`.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT:-8000}"]
