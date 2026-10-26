# Flower SuperNode with the org ClientApp installed.
# Build from the repo root (needs libs/fl-common):
#   docker build -f services/client/Dockerfile -t honeypot-fl-client .
#
# Debian-based on purpose: the official flwr/supernode image is Alpine (musl),
# and PyTorch's Linux wheels need glibc, so `import torch` fails there with
# "Error relocating ... libgomp ... symbol not found".
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

# Same venv location as the official Flower images.
ENV VIRTUAL_ENV=/python/venv \
    PATH=/python/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
RUN uv venv /python/venv --python /usr/local/bin/python

WORKDIR /app
COPY libs/fl-common ./libs/fl-common
COPY services/client ./services/client

# CPU-only torch keeps the image small (the CUDA build is several GB).
# flwr itself (pinned to 1.40.x) comes in as a dependency of the client.
RUN uv pip install --python /python/venv/bin/python \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        --index-strategy unsafe-best-match \
        ./libs/fl-common ./services/client

# Run as a non-root user, like the official image.
RUN useradd --create-home app && chown -R app:app /app /python
USER app

EXPOSE 9094
ENTRYPOINT ["flower-supernode"]
