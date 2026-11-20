FROM docker:27.5.1-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS docker-cli
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS gateway
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY track_a/*.py /app/track_a/
COPY vision_client.py /app/
CMD ["python3", "-m", "track_a.server"]

FROM gateway AS executor
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
CMD ["python3", "-m", "track_a.executor"]

FROM gateway AS checks
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py /app/
COPY tests/*.py /app/tests/
COPY track_a/baf /app/track_a/baf
COPY track_a/*.json /app/track_a/
COPY results/baf-suite-20261106T015237658406Z /app/recordings/baf-v1
CMD ["python3", "-m", "track_a.checks"]
