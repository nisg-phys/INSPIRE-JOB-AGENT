import sys
from functools import lru_cache

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing = sorted({str(err["loc"][0]) for err in exc.errors() if err["type"] == "missing"})
        if missing:
            sys.stderr.write(
                "worker: missing required environment variable(s): "
                + ", ".join(name.upper() for name in missing)
                + "\nCopy .env.example to .env (repo root) and fill these in.\n"
            )
        else:
            sys.stderr.write(f"worker: invalid configuration:\n{exc}\n")
        sys.exit(1)
