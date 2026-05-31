ARG CUDA_VERSION=12.9.1
ARG PYTHON_VERSION=3.12
ARG FINAL_BASE_IMAGE=nvidia/cuda:${CUDA_VERSION}-base-ubuntu22.04

FROM ${FINAL_BASE_IMAGE}
ARG CUDA_VERSION
ARG PYTHON_VERSION
ARG GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py

ENV DEBIAN_FRONTEND=noninteractive
ENV UV_HTTP_TIMEOUT=500
ENV UV_INDEX_STRATEGY=unsafe-best-match
ENV UV_LINK_MODE=copy
ENV LD_LIBRARY_PATH=/usr/local/nvidia/lib64:/usr/local/cuda/lib64:${LD_LIBRARY_PATH}

SHELL ["/bin/bash", "-c"]

RUN echo 'tzdata tzdata/Areas select America' | debconf-set-selections \
    && echo 'tzdata tzdata/Zones/America select Los_Angeles' | debconf-set-selections \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends \
      software-properties-common \
      curl \
      ca-certificates \
      sudo \
      python3-pip \
      ffmpeg \
      libsm6 \
      libxext6 \
      libgl1 \
      git \
      jq \
    && for i in 1 2 3; do \
      add-apt-repository -y ppa:deadsnakes/ppa && break || { echo "Attempt $i failed, retrying in 5s"; sleep 5; }; \
    done \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends \
      python${PYTHON_VERSION} \
      python${PYTHON_VERSION}-dev \
      python${PYTHON_VERSION}-venv \
      libibverbs-dev \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1 \
    && update-alternatives --set python3 /usr/bin/python${PYTHON_VERSION} \
    && ln -sf /usr/bin/python${PYTHON_VERSION}-config /usr/bin/python3-config \
    && curl -sS ${GET_PIP_URL} | python${PYTHON_VERSION} \
    && python3 -m pip install uv

RUN CUDA_VERSION_DASH=$(echo ${CUDA_VERSION} | cut -d. -f1,2 | tr '.' '-') \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends \
      cuda-nvcc-${CUDA_VERSION_DASH} \
      cuda-cudart-${CUDA_VERSION_DASH} \
      cuda-nvrtc-${CUDA_VERSION_DASH} \
      cuda-cuobjdump-${CUDA_VERSION_DASH} \
      libcurand-dev-${CUDA_VERSION_DASH} \
      libcublas-${CUDA_VERSION_DASH} \
      libnccl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN ldconfig /usr/local/cuda-$(echo ${CUDA_VERSION} | cut -d. -f1,2)/compat/

WORKDIR /vllm-workspace
COPY containers/vllm_server/serve_vllm.sh /app/bin/serve_vllm.sh
RUN chmod +x /app/bin/serve_vllm.sh

CMD ["/app/bin/serve_vllm.sh"]
