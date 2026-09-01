# Зависимостей нет - только стандартная библиотека Python, поэтому ни pip, ни
# слоя с requirements здесь нет намеренно: образ собирается за секунды, весит
# около 50 МБ и в нём нечему протухнуть. Если однажды понадобится библиотека,
# сначала стоит проверить, не решается ли задача стандартной.
FROM python:3.12-alpine

WORKDIR /app

COPY monitor.py .
COPY device_monitor ./device_monitor
COPY static ./static

# Сервис пишет только в /app/data (список устройств), всё остальное read-only.
# Конкретный uid задаётся в docker-compose.yml: каталог data монтируется с
# хоста, и процесс в контейнере должен совпадать с его владельцем, иначе
# запись падает с PermissionError. Здесь только дефолт на случай запуска
# без bind-mount.
RUN adduser -D -u 10001 monitor && mkdir -p /app/data && chown -R monitor /app/data
USER monitor

ENV DM_HOST=127.0.0.1 \
    DM_PORT=8890 \
    DM_DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1

EXPOSE 8890

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['DM_PORT']}/api/health\", timeout=3)"

CMD ["python3", "monitor.py"]
