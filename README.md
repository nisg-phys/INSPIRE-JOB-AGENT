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

Database migrations are applied by hand, not by the deploy: run
`cd backend && alembic upgrade head` against the production `DATABASE_URL`
before merging a change that adds one to `main`.

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
  real crawler that ingests boards into `jobs_raw` would fix all three. Its
  first prerequisite, resolving institution names to Inspire records, now
  exists (`institution_aliases`, below). Still open: search only queries
  Inspire live and never reads `jobs_raw`, so ingested rows would also need
  a search path of their own before any user saw them.
- **Inspire queries that miss postings Inspire actually has.** Searching the
  web for "postdoc in celestial holography" surfaced
  `inspirehep.net/jobs/2844516` ("Simons Fellow in Celestial Holography") - a
  perfect match that our own call to Inspire's jobs API returned zero results
  for, most likely because the posting's rank isn't `POSTDOC` and the rank
  filter is applied as a hard filter. Worth investigating on its own: the web
  fallback currently hides this by finding such postings anyway, which is a
  workaround rather than a fix.
- **The same position listed twice (mostly fixed).** Web results are now
  merged when they point at the same page (URL compared without scheme,
  `www.`, trailing slash, fragment or `utm_*` parameters), or when they come
  from the same employer and one title's topic words all appear in the
  other's - so "PhD Position in Experimental Astroparticle Physics" and
  "... and Neutrino Astronomy" show once (`_dedupe` in
  `backend/app/web_jobs.py`). The matching errs towards keeping rows: career
  stages must match exactly and a partial match needs three shared topic
  words, so mirrors reworded with synonyms, or listed under two spellings of
  the employer, still appear twice. Comparing the destination pages
  themselves would catch those, at the cost of fetching them.
- **Expired web postings.** Inspire rows are filtered by a real `status=open`
  field. Web rows have only a heuristic: a posting is dropped if the LLM
  extracted a deadline that has passed. Most pages state no deadline in their
  snippet, so closed positions do get through. Fetching the page to confirm it
  is still open (Tavily's extract endpoint) would help, at roughly double the
  latency and credits.
- **Prompt-injection defence is untested against a real attacker.** Web page
  text is attacker-controlled and reaches an LLM prompt. It is sanitized,
  fenced in a delimited block the model is told to treat as data, and the
  model's output is structurally validated and re-sanitized before display -
  and critically, the link shown to a user is always the URL the search
  returned, never anything the model produced, so an injection cannot redirect
  anyone. What is missing is adversarial testing against a page written to
  defeat this, and a check that a hostile page cannot get itself listed with a
  plausible-looking title. `backend/tests/test_web_jobs.py` covers the
  mechanics, not a determined attacker.
- **Off-topic refusal depends on one LLM call.** `on_topic` in
  `query_rewriter.SYSTEM_PROMPT` decides whether a query is answered at all.
  It fails closed (an unreadable response is treated as off topic) and is
  checked by `backend/scripts/check_ambiguity.py`, 30/30 at the last run. But
  it is a model judgement, not a rule: it has no deterministic backstop, and
  the boundary for adjacent fields (applied maths, scientific computing) is
  set by prompt wording alone.
- **Career-stage detection for the cache is a keyword list.** A cache entry
  is only served when its query names the same career stages as the incoming
  one (`career_stages` in `backend/app/query_rewriter.py`), so "phd in string
  theory" can no longer be answered with a cached "postdoc in string theory"
  however close their embeddings are (0.77, just under the 0.8 threshold).
  The key is computed from the raw text rather than the LLM's parsed `ranks`
  because the cache is checked before the LLM runs. Its weakness is the
  keyword list itself: an unusual phrasing ("W2 position", "chargé de
  recherche") reads as no stage and quietly skips the cache, and two
  phrasings the list doesn't know are synonyms only cost a miss, never a
  wrong hit.
- **Dashboard runs locally, and there are no alerts.** A Grafana dashboard
  over Cloud Run's metrics and log-based metrics built from the structured
  logs lives in `observability/` (latency percentiles per step, search latency
  split by cache hit, cache hit rate, LLM failover, refusals, web fallback
  failures). It only exists while the local Grafana container is running, and
  nothing pages anyone: hosting it (e.g. Grafana Cloud's free tier) and
  alerting on the symptoms users feel (5xx rate, p95 search latency) rather
  than every internal wobble are still open.
- **Rate limiting is per instance and trusts X-Forwarded-For.**
  `/jobs/search` is capped per client (10/minute, 100/hour by default; see
  `backend/app/rate_limit.py`) so one caller can't spend the shared Tavily
  credits and LLM free-tier quotas for everyone. Two gaps: counts live in each
  Cloud Run instance's memory, so a client spread across N instances gets up
  to N times the limit; and the client is identified by the first
  X-Forwarded-For entry, which the client controls, so a determined caller
  can rotate it. A shared store (e.g. Memorystore), and keying on an address
  a trusted proxy appends rather than the client-supplied first entry, would
  close both.
- **Paper enrichment for free-text institutions (curation left).** Papers
  are fetched by Inspire's institution record id (`affid <id>`) whenever one
  is known. A posting usually links one (~95%); a free-text name now gets one
  from `institution_aliases` (see `worker/worker/aliases.py`), which the
  daily worker fills from every name->id link a posting has made and from
  each institution record's own spellings (legacy ICN, name variants).
  Matching only ignores case, punctuation and spacing - "U. Kentucky" vs
  "Kentucky U." is never guessed, since fuzzy matching ranks the wrong record
  first (`Kentucky State U.` for "U. Kentucky") and showing another
  university's papers is worse than showing none. A free-text name whose
  name search finds nothing lands in `unresolved_institutions`; what's left
  is working through that list by hand (`python -m worker.aliases
  unresolved`, then `... add "<name>" <inspire id>`), after which the next
  worker run re-fetches it by id.
