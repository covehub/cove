FROM python:3.12-slim

WORKDIR /app

COPY containers/common/hello_world_common.py /app/hello_world_common.py
COPY containers/final_server/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/main.py"]
