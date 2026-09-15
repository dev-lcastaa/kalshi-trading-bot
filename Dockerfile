# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# The Python image owns ingestion, prediction, persistence, and the API/WebSocket.
# The React dashboard is built and served separately by frontend/Dockerfile.
COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Secrets and database state are mounted at runtime, not baked into the image.
EXPOSE 8000

CMD ["python", "-m", "kalshi_bot.main"]
