FROM python:3.12-slim

WORKDIR /app

COPY containers/common/hello_world_common.py /app/hello_world_common.py
COPY containers/character_set_checker/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/main.py"]
