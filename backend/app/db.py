from functools import lru_cache

from sqlalchemy import Engine, create_engine

from app.config import get_settings


def _psycopg_url(url: str) -> str:
    # sqlalchemy's plain "postgresql://" scheme defaults to psycopg2, but this
    # project depends on psycopg (v3) - force that driver explicitly.
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


@lru_cache
def get_engine() -> Engine:
    # Neon (and most serverless/pooled Postgres) can close an idle connection
    # server-side without SQLAlchemy's pool knowing - the next request to
    # reuse it then fails outright with "SSL connection has been closed
    # unexpectedly", unrelated to whatever that request was actually doing.
    # pool_pre_ping issues a cheap liveness check before handing out a
    # pooled connection and transparently reconnects if it's dead;
    # pool_recycle proactively retires connections before they get that
    # stale in the first place.
    return create_engine(
        _psycopg_url(get_settings().database_url),
        pool_pre_ping=True,
        pool_recycle=300,
    )
