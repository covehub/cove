FROM node:20-alpine AS frontend

WORKDIR /src/ui

COPY ui/package.json ./
COPY ui/package-lock.json ./
RUN npm install --no-audit --no-fund

COPY ui/index.html ./index.html
COPY ui/src ./src
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COVE_UI_DATA_ROOT=/var/lib/covehub/data \
    COVE_UI_STATIC_ROOT=/app/static \
    COVE_UI_DOCS_ROOT=/app/docs

WORKDIR /app

COPY ui /tmp/ui
COPY docs /app/docs
COPY --from=frontend /src/ui/dist /app/static

RUN pip install --no-cache-dir /tmp/ui \
    && mkdir -p /var/lib/covehub/data

EXPOSE 8080

CMD ["uvicorn", "covehub_ui.app:app", "--host", "0.0.0.0", "--port", "8080"]
