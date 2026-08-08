FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MDM_HOST=0.0.0.0 \
    MDM_PORT=5000 \
    MDM_PRODUCTION=1 \
    MDM_DB_PATH=/app/data/mdm_data.db \
    MDM_LOG_DIR=/app/logs \
    MDM_SECURITY_MODE=enterprise \
    MDM_SECURITY_DIR=/app/data \
    MDM_RAG_SOURCE_PATH=/app/SY_T5497-2018备品备件分类树(1).xlsx

WORKDIR /app

COPY backend/requirements.txt backend/requirements-ocr.txt /app/backend/
ARG INSTALL_OCR=0
RUN python -m pip install --no-cache-dir -r /app/backend/requirements.txt && \
    if [ "$INSTALL_OCR" = "1" ]; then python -m pip install --no-cache-dir -r /app/backend/requirements-ocr.txt; fi

COPY backend /app/backend
COPY evaluation /app/evaluation
COPY ["备品备件脏数据.csv", "/app/备品备件脏数据.csv"]
COPY ["SY_T5497-2018备品备件分类树(1).xlsx", "/app/SY_T5497-2018备品备件分类树(1).xlsx"]
RUN mkdir -p /app/data /app/logs

WORKDIR /app/backend
EXPOSE 5000
VOLUME ["/app/data", "/app/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3)"

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "app:app"]
