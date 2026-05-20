FROM python:3.14-slim

ENV PLATFORMIO_HOME_DIR=/pio-home

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir platformio poetry

WORKDIR /app
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-root --no-interaction
COPY app/ ./app/

RUN groupadd -g 1000 builder && \
    useradd -u 1000 -g 1000 -m -d /home/builder builder && \
    mkdir -p /pio-home /app/downloads && \
    chown -R 1000:1000 /pio-home /app

USER 1000:1000

CMD ["fastapi", "run", "app/main.py", "--port", "8000"]
