# Базовый образ с Python 3.11 (совместим с Flask 3 и timezonefinder 6.6.x)
FROM python:3.11-slim

# Системные пакеты (на всякий случай для сборки wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала только зависимости — чтобы кеш слоёв работал корректно
COPY requirements.txt /app/requirements.txt

# Обновляем pip и ставим зависимости без кеша
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r /app/requirements.txt

# Теперь кладём весь код
COPY . /app

# Порт отдаёт Railway через переменную PORT
ENV PORT=8080

# Старт: подтянуть эфемериды и запустить gunicorn
CMD bash -lc "python fetch_ephe.py && gunicorn app:app -k gthread --threads 4 --bind 0.0.0.0:${PORT}"
