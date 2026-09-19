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
- **Fix institution-matching for paper enrichment.** Some institutions (e.g.
  U. Kentucky) get zero recent papers even though the institution clearly
  has output on Inspire. Cause: `institution_papers` enrichment matches
  papers via Inspire's literature API using an exact-phrase affiliation
  search (`aff "<institution name>"`), and the institution name string on a
  job posting doesn't always match the affiliation string on that
  institution's papers verbatim. Needs a fuzzier/normalized matching
  strategy (alias table, fuzzy matching, or Inspire's institution record IDs
  instead of raw name strings) rather than exact-phrase matching.
