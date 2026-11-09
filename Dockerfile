# FastAPI + uvicorn. Раньше был pure-stdlib без зависимостей; при переходе на
# FastAPI/SQLAlchemy/Pydantic зависимости появились — ставятся из requirements.
FROM python:3.12-slim

WORKDIR /app

# Сначала зависимости — отдельным слоем, чтобы правки кода не пересобирали их.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .
COPY device_monitor ./device_monitor
COPY static ./static

# Сервис пишет только в /app/data (база). Владелец задаётся в docker-compose
# через user: — каталог монтируется с хоста, и uid процесса должен совпадать
# с владельцем каталога, иначе запись падает. Здесь дефолт на случай без mount.
RUN adduser --disabled-password --uid 10001 monitor && mkdir -p /app/data && chown -R monitor /app/data
USER monitor

ENV DM_HOST=127.0.0.1 \
    DM_PORT=8890 \
    DM_DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1

EXPOSE 8890

HEALTHCHECK --interval=30s --timeout=5s --start-period=8s --retries=3 \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['DM_PORT']}/api/v1/health\", timeout=3)"

CMD ["python3", "monitor.py"]
