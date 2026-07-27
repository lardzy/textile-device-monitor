FROM python:3.11-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    libreoffice-core \
    libreoffice-calc \
    python3-uno \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

EXPOSE 8000

ENV FORWARDED_ALLOW_IPS=127.0.0.1

CMD ["sh", "-c", "python -m app.db_migrations && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"$FORWARDED_ALLOW_IPS\""]
