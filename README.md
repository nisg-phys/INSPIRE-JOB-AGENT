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

## Future scope

- **Web crawler for job boards beyond Inspire-HEP.** Currently the only source
  of job postings is Inspire-HEP's own jobs API. Many relevant postings (e.g.
  university HR portals, AcademicJobsOnline listings not mirrored on Inspire)
  never show up. A crawler that ingests additional job boards, normalizes
  them into the same `RawJob`/`FormattedJob` shape, and feeds them through
  the same query/cache/enrichment path would meaningfully widen coverage.
- **Paper enrichment for free-text institutions (partly fixed).** Papers are
  now fetched by Inspire's institution record id (`affid <id>`) whenever the
  job posting links one (~95% of open postings), which fixes the original
  problem of the same institution being spelled differently across postings
  ("Kentucky U." vs "U. Kentucky") and matching nothing. What remains: the
  ~5% of institution entries that are free text with no linked record still
  fall back to an exact-phrase name search, so a non-canonical spelling (e.g.
  a posting that writes "U. Kentucky") still gets no papers. Resolving those
  by fuzzy-matching Inspire's institution search was rejected on purpose: it
  ranks the wrong record first (`Kentucky State U.` for "U. Kentucky"), and
  showing another university's papers is worse than showing none. A curated
  alias table would be the safe way to close this gap.
