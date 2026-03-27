FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

ENV DB_PATH=/data/pr_context.db

VOLUME /data

ENTRYPOINT ["python", "-m", "pr_context"]
