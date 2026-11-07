FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY tools/cowrie_dataset/cowrie_sessions.py /app/tools/cowrie_dataset/cowrie_sessions.py
COPY libs/fl-common/src/fl_common/__init__.py libs/fl-common/src/fl_common/schema.py /app/libs/fl-common/src/fl_common/
COPY services/log-processor/process_replay.py /app/services/log-processor/process_replay.py
USER 999:999
ENTRYPOINT ["python", "/app/services/log-processor/process_replay.py"]
