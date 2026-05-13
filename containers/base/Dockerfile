FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY cove_container_runtime /src/cove_container_runtime

RUN python -m pip install --no-cache-dir /src/cove_container_runtime \
    && rm -rf /src/cove_container_runtime
