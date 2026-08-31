FROM python:3.11-slim AS builder

WORKDIR /app

# System deps needed by packages without prebuilt wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


FROM python:3.11-slim AS runtime

WORKDIR /app
ENV PYTHONPATH=/app:/app/backend

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY . .

RUN adduser --disabled-password --gecos "" appuser \
 && mkdir -p logs \
 && chmod +x docker-entrypoint.sh \
 && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health/ready || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "backend.api:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
