# Build from the repository root:  docker build -f api/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY api/requirements.txt /app/api/requirements.txt
RUN pip install -r /app/api/requirements.txt

COPY api/*.py /app/api/
COPY api/agents/*.py /app/api/agents/
COPY api/orchestrator/*.py /app/api/orchestrator/

RUN useradd --uid 10001 --no-create-home appuser
USER 10001

CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
