ARG BUILD_BASE_IMAGE=vllm/vllm-openai:latest

FROM ${BUILD_BASE_IMAGE}
ARG CUDA_VERSION=12.8.1
ARG PYTORCH_CUDA_INDEX_BASE_URL=https://download.pytorch.org/whl
ARG TORCH_CUDA_ARCH_LIST="7.0 7.5 8.0 8.9 9.0 10.0 12.0"
ARG MAX_JOBS=20
ARG NVCC_THREADS=1

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTORCH_CUDA_INDEX_BASE_URL=${PYTORCH_CUDA_INDEX_BASE_URL}
ENV TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST}
ENV MAX_JOBS=${MAX_JOBS}
ENV NVCC_THREADS=${NVCC_THREADS}
ENV VLLM_TARGET_DEVICE=cuda
ENV SETUPTOOLS_SCM_PRETEND_VERSION="0.0.0+csrc.build"
ENV UV_HTTP_TIMEOUT=500
ENV UV_INDEX_STRATEGY=unsafe-best-match
ENV UV_LINK_MODE=copy

SHELL ["/bin/bash", "-c"]

RUN echo 'tzdata tzdata/Areas select America' | debconf-set-selections \
    && echo 'tzdata tzdata/Zones/America select Los_Angeles' | debconf-set-selections \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends \
      ccache \
      software-properties-common \
      git \
      curl \
      ca-certificates \
      sudo \
      python3-pip \
      python3-venv \
      python3-dev \
      build-essential \
      ninja-build \
      libibverbs-dev \
      jq \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir uv

RUN cuda_compat="/usr/local/cuda-$(echo ${CUDA_VERSION} | cut -d. -f1,2)/compat" \
    && if [[ -d "${cuda_compat}" ]]; then ldconfig "${cuda_compat}"; fi

WORKDIR /workspace
COPY containers/vllm_compiler/compile_vllm.sh /app/bin/compile_vllm.sh
RUN chmod +x /app/bin/compile_vllm.sh

CMD ["/app/bin/compile_vllm.sh"]
