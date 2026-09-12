# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# System deps: none required - psycopg[binary] ships its own libpq, cryptography
# ships its own wheel with OpenSSL bundled for this base image.
COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Secrets (private key) and the local SQLite fallback db are mounted as
# volumes at runtime, not baked into the image - see docker-compose.yml.
EXPOSE 8000

CMD ["python", "-m", "kalshi_bot.main"]
