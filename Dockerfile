FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EPHE_PATH=/app/ephe \
    DEBUG_ROUTES=false

WORKDIR /app

# соль для форс-ребилда
ARG CACHEBUSTER=2025-08-21-0228

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# важны флаги: сначала обновить инструменты, потом просить бинарные колёса
RUN python -m pip install -U pip setuptools wheel && \
    pip install --prefer-binary --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8080
CMD ["python", "app.py"]
