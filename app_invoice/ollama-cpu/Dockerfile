# Image Ollama CPU uniquement (sans CUDA/ROCm/MLX), bien plus légère que ollama/ollama
# OLLAMA_VERSION vide : dernière version ; sinon cette version exacte (ex. 0.40.16), pour la mise en production
FROM python:3.12-slim
ARG OLLAMA_VERSION=
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates zstd \
 && if [ -n "$OLLAMA_VERSION" ]; then \
      url="https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst"; \
    else url="https://ollama.com/download/ollama-linux-amd64.tar.zst"; fi \
 && curl -fsSL "$url" \
    | zstd -d | tar -x -C /usr --exclude='lib/ollama/cuda*' --exclude='lib/ollama/mlx*' \
      --exclude='lib/ollama/rocm*' --exclude='lib/ollama/vulkan*' \
 && test -x /usr/bin/ollama \
 && apt-get purge -y curl zstd && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434
ENTRYPOINT ["/usr/bin/ollama"]
CMD ["serve"]
