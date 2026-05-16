FROM python:3.12-slim

ARG MESHCORE_REPO=https://github.com/meshcore-dev/MeshCore.git
ARG MESHCORE_REF=main

ENV MESHCORE_REPO=${MESHCORE_REPO} \
    MESHCORE_REF=${MESHCORE_REF} \
    PLATFORMIO_HOME_DIR=/pio-home

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir platformio

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

RUN groupadd -g 1000 builder && \
    useradd -u 1000 -g 1000 -m -d /home/builder builder && \
    mkdir -p /pio-home /app/downloads && \
    chown -R 1000:1000 /pio-home /app

USER 1000:1000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
