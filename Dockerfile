# Было: FROM python:3.11-slim
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EPHE_PATH=/app/ephe \
    DEBUG_ROUTES=false

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# важен апгрейд pip/setuptools/wheel на «чистом» образе
RUN python -m pip install -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8080
CMD ["python", "app.py"]
