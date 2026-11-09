# Image Ollama CPU uniquement (sans CUDA/ROCm/MLX), bien plus légère que ollama/ollama
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates zstd \
 && curl -fsSL https://ollama.com/download/ollama-linux-amd64.tar.zst \
    | zstd -d | tar -x -C /usr --exclude='lib/ollama/cuda*' --exclude='lib/ollama/mlx*' \
      --exclude='lib/ollama/rocm*' --exclude='lib/ollama/vulkan*' \
 && apt-get purge -y curl zstd && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434
ENTRYPOINT ["/usr/bin/ollama"]
CMD ["serve"]
