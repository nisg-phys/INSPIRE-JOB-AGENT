"""Shared test setup.

Disables Opik tracing before any app module is imported, so the
@opik.track decorators on LLM call sites (app/llm/*, query_rewriter,
etc.) don't try to reach the network during unit tests, regardless of
whether OPIK_API_KEY happens to be set in the environment running them.
"""

import os

os.environ["OPIK_TRACK_DISABLE"] = "True"
