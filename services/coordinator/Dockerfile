# Flower SuperLink with the coordinator ServerApp installed.
# Build from the repo root (needs libs/fl-common):
#   docker build -f services/coordinator/Dockerfile -t honeypot-fl-coordinator .
FROM flwr/superlink:1.40.0

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

USER root
WORKDIR /app

COPY libs/fl-common ./libs/fl-common
COPY services/coordinator ./services/coordinator

# CPU-only torch keeps the image small (the CUDA build is several GB).
RUN uv pip install --python /python/venv/bin/python \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        --index-strategy unsafe-best-match \
        ./libs/fl-common ./services/coordinator

ENTRYPOINT ["flower-superlink"]
