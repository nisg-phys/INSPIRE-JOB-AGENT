"""Opik tracing setup.

Opik's SDK reads its own OPIK_* environment variables (separate from our
Settings' .env loading, which only populates our Settings object, not the
process environment) - so this bridges our config into the process env
before any @opik.track-decorated function runs. Call setup_tracing() once
at startup, before the app starts handling requests.

If no Opik API key is configured, tracing is explicitly disabled rather
than left to fail/prompt - this isn't required just to run the app.
"""

from __future__ import annotations

import os

from app.config import get_settings


def setup_tracing() -> None:
    settings = get_settings()
    if not settings.opik_api_key:
        os.environ["OPIK_TRACK_DISABLE"] = "True"
        return

    os.environ["OPIK_API_KEY"] = settings.opik_api_key
    os.environ["OPIK_WORKSPACE"] = settings.opik_workspace
    os.environ["OPIK_PROJECT_NAME"] = settings.opik_project_name
