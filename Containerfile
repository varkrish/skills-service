FROM registry.access.redhat.com/ubi9/python-311:latest AS builder

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.29.0" \
    "llama-index-core>=0.10.0" \
    "llama-index-embeddings-huggingface>=0.1.0" \
    "pydantic>=2.0.0" \
    "pyyaml>=6.0.0" \
    "fastmcp>=2.0.0"

# Pre-download the embedding model at build time
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

COPY src/ ./src/

WORKDIR /app/src

EXPOSE 8090

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
