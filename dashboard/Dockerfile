# Build from the repository root:  docker build -f dashboard/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY dashboard/requirements.txt /app/dashboard/requirements.txt
RUN pip install -r /app/dashboard/requirements.txt

COPY dashboard/*.py /app/dashboard/
COPY dashboard/static/ /app/dashboard/static/

RUN useradd --uid 10001 --no-create-home appuser
USER 10001

CMD ["sh", "-c", "exec uvicorn dashboard.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
