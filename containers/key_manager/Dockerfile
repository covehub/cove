ARG COVE_BASE_IMAGE=cove-base:dev
FROM ${COVE_BASE_IMAGE}

COPY containers/key_manager/main.py /app/main.py

ENTRYPOINT ["python", "/app/main.py"]
