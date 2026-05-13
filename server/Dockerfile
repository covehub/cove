FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COVE_SERVER_HOST=0.0.0.0 \
    COVE_SERVER_PORT=8000 \
    COVE_SERVER_DATA_ROOT=/var/lib/covehub/data

WORKDIR /app

COPY cove_container_runtime /tmp/cove_container_runtime
COPY server /tmp/server

RUN pip install --no-cache-dir /tmp/cove_container_runtime /tmp/server \
    && mkdir -p /var/lib/covehub/data

EXPOSE 8000

CMD ["covehub-server"]
