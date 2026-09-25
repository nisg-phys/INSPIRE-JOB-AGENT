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

- **Job sources beyond Inspire-HEP (partly done).** Inspire-HEP's jobs API is
  still the primary source, and postings on university HR portals or job
  boards that never reach Inspire used to be invisible. A web-search fallback
  now covers the worst of that gap: when a search returns zero Inspire jobs,
  `backend/app/web_jobs.py` searches the web via Tavily, has the LLM verify
  each hit is a genuine, matching job posting, and renders the survivors in
  the same table marked "Web result". What remains: web rows are found live
  per query, never ingested, so they aren't deduped against Inspire postings,
  aren't available for paper enrichment (their institution names aren't
  Inspire's canonical spellings), and can't be refreshed on a schedule. A
  real crawler that ingests boards into `jobs_raw` would fix all three - note
  it would need its own institution-name normalization first, since
  `worker/worker/discovery.py` would otherwise chase those names forever.
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
