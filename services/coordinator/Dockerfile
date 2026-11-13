# Flower SuperLink with the coordinator ServerApp installed.
# Build from the repo root (needs libs/fl-common):
#   docker build -f services/coordinator/Dockerfile -t honeypot-fl-coordinator .
#
# Debian-based on purpose: the official flwr/superlink image is Alpine (musl),
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
COPY services/coordinator ./services/coordinator
# Only so `flwr run` (the daily trigger) can build the app: it imports BOTH
# components named in deploy/flower-app/pyproject.toml before submitting.
# The coordinator never runs client code.
COPY services/client ./services/client

# CPU-only torch keeps the image small (the CUDA build is several GB).
# flwr itself (pinned to 1.40.x) comes in as a dependency of the coordinator.
RUN uv pip install --python /python/venv/bin/python \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        --index-strategy unsafe-best-match \
        ./libs/fl-common ./services/coordinator ./services/client

# The Flower app definition, so the same image can run the daily trigger as a
# one-off task:  python -m fl_coordinator.trigger   (see fl_coordinator/trigger.py)
COPY deploy/flower-app ./flower-app

# AWS start-up (TLS + SuperLink + SuperNode re-registration): the ECS task
# definition runs /app/aws-start.sh. The default ENTRYPOINT below is for local use.
COPY --chmod=755 services/coordinator/aws-start.sh ./aws-start.sh

# Run as a non-root user, like the official image.
RUN useradd --create-home app && chown -R app:app /app /python
USER app

EXPOSE 8000
ENTRYPOINT ["flower-superlink"]
