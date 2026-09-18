import sys

from worker.config import get_settings
from worker.enrichment import enrich


def main() -> None:
    get_settings()

    institutions = sys.argv[1:]
    if not institutions:
        print("usage: python -m worker.main <institution> [<institution> ...]", file=sys.stderr)
        sys.exit(1)

    enrich(institutions)


if __name__ == "__main__":
    main()
