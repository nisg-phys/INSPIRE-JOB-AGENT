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
- **Inspire queries that miss postings Inspire actually has.** Searching the
  web for "postdoc in celestial holography" surfaced
  `inspirehep.net/jobs/2844516` ("Simons Fellow in Celestial Holography") - a
  perfect match that our own call to Inspire's jobs API returned zero results
  for, most likely because the posting's rank isn't `POSTDOC` and the rank
  filter is applied as a hard filter. Worth investigating on its own: the web
  fallback currently hides this by finding such postings anyway, which is a
  workaround rather than a fix.
- **The same position listed twice.** Web results are deduplicated on title
  plus employer, so one posting mirrored with different wording still appears
  twice - e.g. "PhD Position in Experimental Astroparticle Physics" and
  "... and Neutrino Astronomy" from the same institute. Fuzzy title matching,
  or comparing the destination page rather than the search snippet, would
  close this.
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
- **No per-client rate limiting.** Nothing stops one caller from spending the
  shared Tavily credits or the LLM providers' free-tier quotas for everyone.
  A refused off-topic query is cheap (one LLM call, no web search), but it is
  still a call.
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
