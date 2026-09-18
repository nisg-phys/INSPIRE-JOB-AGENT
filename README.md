# Inspire Jobs Agent

Natural-language search over Inspire-HEP job listings, with semantic caching and
institution enrichment.

## Structure

- `backend/` — FastAPI app
- `worker/` — decoupled enrichment worker
- `frontend/` — static frontend
- `shared/` — schema/types shared between backend and worker

## Local dev

```
docker-compose up
```

Copy `.env.example` to `.env` and fill in required values before running any service.
