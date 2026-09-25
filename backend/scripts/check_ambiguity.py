"""Manual regression check for the query rewriter's ambiguity behaviour.

The rules for when to ask a clarifying question live in a prompt
(query_rewriter.SYSTEM_PROMPT), which unit tests can't exercise and CI can't
run (it needs real LLM API keys). Run this by hand after editing the prompt:

    cd backend && .venv/bin/python scripts/check_ambiguity.py

Each case is (query, should_ask_a_clarifying_question).
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("OPIK_TRACK_DISABLE", "True")

from app.query_rewriter import QueryRewriteError, analyze_query  # noqa: E402

CASES = [
    # A subfield with no career stage: ask which one.
    ("string theory", True),
    ("string thoery", True),
    ("cosmology", True),
    ("condensed matter jobs", True),
    ("jobs in lattice QCD", True),
    ("dark matter positions", True),
    # A career stage is given (typos included): just search.
    ("postdoc in string theory", False),
    ("post docs in string thoery", False),
    ("faculty jobs in cosmology", False),
    ("PhD positions in particle physics in Europe", False),
    ("assistant professor astrophysics", False),
    ("research scientist in instrumentation", False),
    ("summer internship in astrophysics", False),
    # The user explicitly asked for everything: nothing left to ask.
    ("any string theory jobs", False),
    ("string theory positions at all career levels", False),
    ("any physics jobs", False),
    ("show me everything open", False),
    # A stage with no subfield is a deliberate broad choice.
    ("postdoc jobs", False),
]


def run(case):
    query, expected = case
    for _ in range(3):  # tolerate a transient provider/network failure
        try:
            return query, expected, analyze_query(query)
        except QueryRewriteError:
            time.sleep(3)
    return query, expected, None


def main() -> int:
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, CASES))

    failures = 0
    for query, expected, parsed in results:
        if parsed is None:
            failures += 1
            print(f"ERR   all providers failed: {query!r}")
        elif parsed.ambiguous != expected:
            failures += 1
            print(f"FAIL  asked={parsed.ambiguous} expected={expected}  {query!r}")
        else:
            print(f"ok    asked={parsed.ambiguous!s:5} ranks={parsed.ranks!s:20} {query!r}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
