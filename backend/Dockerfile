FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libmagic1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY app ./app
# `embeddings` pulls sentence-transformers and torch, which is what the locked
# 768-dimension multilingual-e5-base model needs. It is a large download; set
# BIDREADY_EMBEDDING_PROVIDER=null for a fast first boot with the hashing
# embedder, which is fine for a walkthrough and useless for real retrieval.
ARG INSTALL_EMBEDDINGS=1
RUN if [ "$INSTALL_EMBEDDINGS" = "1" ]; then \
      pip install --no-cache-dir ".[dev,embeddings]"; \
    else \
      pip install --no-cache-dir ".[dev]"; \
    fi
COPY . .
