from functools import lru_cache

from sqlalchemy import Engine, create_engine

from worker.config import get_settings


def _psycopg_url(url: str) -> str:
    # sqlalchemy's plain "postgresql://" scheme defaults to psycopg2, but this
    # project depends on psycopg (v3) - force that driver explicitly.
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


@lru_cache
def get_engine() -> Engine:
    # See backend/app/db.py's get_engine for why: Neon can close an idle
    # pooled connection server-side, and a run here makes sequential DB
    # writes with an Inspire API call between each - long enough for that
    # to happen mid-run without pre_ping/recycle.
    return create_engine(
        _psycopg_url(get_settings().database_url),
        pool_pre_ping=True,
        pool_recycle=300,
    )
