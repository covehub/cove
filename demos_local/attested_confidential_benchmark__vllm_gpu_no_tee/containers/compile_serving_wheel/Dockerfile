ARG BASE_IMAGE
FROM ${BASE_IMAGE}

RUN python3 -m pip install --no-cache-dir "cryptography>=45,<46"

COPY runner/local_node_wrapper.py /app/local_node_wrapper.py

ENTRYPOINT ["python3", "/app/local_node_wrapper.py"]
