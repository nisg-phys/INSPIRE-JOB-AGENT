import sys

from worker.config import get_settings
from worker.discovery import InstitutionRef, discover_institutions
from worker.enrichment import enrich


def main() -> None:
    get_settings()

    # Explicit institutions on the command line still work (useful for
    # manual testing/backfills, fetched by name since no id is known); with
    # none given, discover automatically from institutions seen in jobs_raw.
    institutions = [InstitutionRef(name) for name in sys.argv[1:]] or discover_institutions()
    if not institutions:
        print("worker: no institutions to enrich (none discovered, none passed).")
        return

    enrich(institutions)


if __name__ == "__main__":
    main()
