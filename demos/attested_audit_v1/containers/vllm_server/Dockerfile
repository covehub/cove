ARG FINAL_BASE_IMAGE=vllm/vllm-openai:latest

FROM ${FINAL_BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive
ENV VLLM_TARGET_DEVICE=cuda

SHELL ["/bin/bash", "-c"]

WORKDIR /vllm-workspace
COPY containers/vllm_server/serve_vllm.sh /app/bin/serve_vllm.sh
RUN chmod +x /app/bin/serve_vllm.sh

ENTRYPOINT []
CMD ["/app/bin/serve_vllm.sh"]
