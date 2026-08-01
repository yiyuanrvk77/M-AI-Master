FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MDM_HOST=0.0.0.0 \
    MDM_PORT=5000 \
    MDM_PRODUCTION=1 \
    MDM_DB_PATH=/app/data/mdm_data.db \
    MDM_LOG_DIR=/app/logs

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
RUN mkdir -p /app/data /app/logs

WORKDIR /app/backend
EXPOSE 5000
VOLUME ["/app/data", "/app/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3)"

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "app:app"]
