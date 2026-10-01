# Официальный образ уже содержит Chromium/Firefox/WebKit и все системные зависимости
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

WORKDIR /srv/app

# Xvfb нужен, чтобы запускать браузер в headful-режиме (headless=False)
# внутри контейнера без физического дисплея — это снижает вероятность
# детекта автоматизации некоторыми антибот-системами.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Браузеры уже есть в базовом образе, но на случай обновления версии playwright:
RUN playwright install --with-deps chromium

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

# Запускаем через xvfb-run, чтобы headless=False работал в контейнере без дисплея
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24", "python", "-m", "app.main"]
