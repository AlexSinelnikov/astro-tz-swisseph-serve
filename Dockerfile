FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EPHE_PATH=/app/ephe \
    DEBUG_ROUTES=false

WORKDIR /app

# ломаем кэш на всякий случай
ARG CACHEBUSTER=2025-08-21-0245

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install -U pip setuptools wheel && \
    pip install --prefer-binary --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8080

# Запускаем gunicorn напрямую (никаких Procfile)
# ${PORT:-8080} подставит Railway PORT, а локально — 8080
CMD ["sh","-c","gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:${PORT:-8080} app:app"]
