# Build from the repository root:  docker build -f etl/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY etl/requirements.txt /app/etl/requirements.txt
RUN pip install -r /app/etl/requirements.txt

COPY etl/*.py /app/etl/

RUN useradd --uid 10001 --no-create-home appuser
USER 10001

CMD ["python", "-m", "etl.main"]
