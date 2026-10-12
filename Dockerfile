# Официальный образ уже содержит Chromium/Firefox/WebKit и все системные зависимости
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

WORKDIR /srv/app

# PYTHONUNBUFFERED — логи сразу попадают в `docker compose logs`, а не висят в буфере.
# PYTHONDONTWRITEBYTECODE — не мусорим .pyc внутри контейнера.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Xvfb нужен, чтобы запускать браузер в headful-режиме (headless=False)
# внутри контейнера без физического дисплея — это снижает вероятность
# детекта автоматизации некоторыми антибот-системами.
# x11vnc — чтобы подключаться к виртуальному экрану по VNC (через SSH-туннель)
# и вручную проходить проверку «я не робот» при авторизации.
# tzdata — чтобы работал TZ из .env (иначе время в логах всегда UTC).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Браузеры уже есть в базовом образе, но на случай обновления версии playwright:
RUN DEBIAN_FRONTEND=noninteractive playwright install --with-deps chromium

COPY app ./app
# tools/ и tests/ — чтобы проверить конфиг и прогнать тесты прямо в контейнере
# на сервере (./deploy.sh check и ./deploy.sh test), без Python на хосте.
COPY tools ./tools
COPY tests ./tests

RUN mkdir -p /data
VOLUME ["/data"]

CMD ["bash", "-c", "rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1920x1080x24 -ac & sleep 1; export DISPLAY=:99; if [ -n \"$VNC_PASSWORD\" ]; then x11vnc -display :99 -forever -shared -passwd \"$VNC_PASSWORD\" -quiet -bg; fi; exec python -m app.main"]