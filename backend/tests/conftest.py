"""Shared test setup.

Disables Opik tracing before any app module is imported, so the
@opik.track decorators on LLM call sites (app/llm/*, query_rewriter,
etc.) don't try to reach the network during unit tests, regardless of
whether OPIK_API_KEY happens to be set in the environment running them.

Also supplies placeholder config. app/main.py calls get_settings() at
import time and exits if anything required is missing, so without this
the endpoint tests couldn't import it - CI deliberately runs with no
real keys. Nothing here is ever used to reach a real service: the tests
that import main.py patch every call that would leave the process.
"""

import os

os.environ["OPIK_TRACK_DISABLE"] = "True"

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
for _placeholder in ("GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
    os.environ.setdefault(_placeholder, "test-key-not-used")
