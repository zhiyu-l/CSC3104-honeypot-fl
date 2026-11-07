FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir boto3==1.43.123
COPY tools/cowrie_dataset/cowrie_sessions.py /app/tools/cowrie_dataset/cowrie_sessions.py
COPY libs/fl-common/src/fl_common/__init__.py libs/fl-common/src/fl_common/schema.py /app/libs/fl-common/src/fl_common/
COPY services/log-processor/process_replay.py services/log-processor/nightly.py /app/services/log-processor/
USER 999:999
ENTRYPOINT ["python", "/app/services/log-processor/nightly.py"]
