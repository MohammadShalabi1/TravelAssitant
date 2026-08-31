# Rihla Travel Assistant

Rihla is a FastAPI backend with a React frontend for an AI travel assistant.

## Backend Python Support

The backend is developed and deployed on Python 3.11.

## Backend Setup

Install runtime dependencies:

```bash
python -m pip install -r requirements.txt
```

Install development and verification dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Required backend environment variables:

```bash
DATABASE_URL=postgresql://user:password@host:5432/database
GEMINI_API_KEY=your-gemini-api-key
JWT_SECRET=change-me
```

Run database migrations before serving the API:

```bash
alembic upgrade head
```

Run the API locally:

```bash
uvicorn backend.api:app --host 0.0.0.0 --port 8000
```

## API Compatibility

Public application routes are canonical under `/api/v1`. Legacy `/api/...`
aliases remain for one release and return deprecation headers. Liveness and
readiness stay outside API versioning at `/health/live` and `/health/ready`.

`POST /api/v1/chat` accepts an optional `Idempotency-Key` header. New clients
should use `POST /api/v1/chat/stream` for Server-Sent Events streaming.

Run security tests:

```bash
python -m unittest discover -s tests/security -p "test_*.py" -v
```

Run dependency vulnerability scanning:

```bash
python -m pip_audit -r requirements.txt
```
