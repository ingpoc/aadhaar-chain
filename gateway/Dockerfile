FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

ENV HOST=0.0.0.0
ENV PORT=10000

EXPOSE 10000

USER appuser

CMD ["sh", "-c", "exec uvicorn main:app --host ${HOST:-0.0.0.0} --port ${PORT:-10000}"]
