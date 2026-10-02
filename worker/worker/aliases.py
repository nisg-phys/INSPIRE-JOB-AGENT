"""Builds institution_aliases: which spellings name which INSPIRE institution.

Resolution itself is the resolve_institution SQL function (see the
add_institution_aliases migration); this module fills the table it reads
from three sources:

- posting: a job posting that linked a name to an institution record. The
  bulk of the table, and free - it is already in jobs_raw.
- record: the spellings an institution record lists for itself (its
  legacy ICN, name variants), fetched once per id from INSPIRE.
- curated: added by hand from the unresolved_institutions worklist, via
  this module's command line. Overrides the other two.

Nothing here guesses. A spelling either appears verbatim in one of those
sources or it stays unresolved.

    python -m worker.aliases unresolved          # the curation worklist
    python -m worker.aliases add "U. Kentucky" 904048
    python -m worker.aliases remove "U. Kentucky" 904048
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from worker.db import get_engine
from worker.inspire_literature_client import InspireAPIError, institution_record

# Records fetched per run. Each is one INSPIRE request; the backlog drains
# over a few daily runs instead of hammering the API on the first.
MAX_RECORD_FETCHES = 50


def sync_posting_aliases() -> int:
    """Record every name->id link a logged job posting made. Returns rows added."""
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO institution_aliases (alias, institution_id, name, source)
                SELECT DISTINCT ON (normalize_institution_name(n), payload->'institution_ids'->>n)
                       normalize_institution_name(n), payload->'institution_ids'->>n, n, 'posting'
                FROM jobs_raw, unnest(institutions) AS n
                WHERE payload->'institution_ids'->>n IS NOT NULL
                  AND normalize_institution_name(n) <> ''
                ON CONFLICT DO NOTHING
                """
            )
        )
        return result.rowcount


def _ids_missing_record_aliases(limit: int) -> list[str]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT institution_id
                FROM institution_aliases a
                WHERE NOT EXISTS (
                    SELECT 1 FROM institution_aliases r
                    WHERE r.institution_id = a.institution_id AND r.source = 'record'
                )
                ORDER BY institution_id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        return [row[0] for row in rows]


def _insert_aliases(institution_id: str, names: list[str], source: str) -> None:
    with get_engine().begin() as conn:
        for name in dict.fromkeys(names):
            conn.execute(
                text(
                    """
                    INSERT INTO institution_aliases (alias, institution_id, name, source)
                    SELECT normalize_institution_name(:name), :institution_id, :name, :source
                    WHERE normalize_institution_name(:name) <> ''
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"name": name, "institution_id": institution_id, "source": source},
            )


def sync_record_aliases(limit: int = MAX_RECORD_FETCHES) -> int:
    """Add each known institution's own spellings from its INSPIRE record.

    Only for ids that have none yet, so each record is fetched once. Every
    record has a legacy ICN, so a successful fetch always leaves a 'record'
    row behind and the id isn't picked again. Returns records fetched.
    """
    fetched = 0
    for institution_id in _ids_missing_record_aliases(limit):
        try:
            record = institution_record(institution_id)
        except InspireAPIError as exc:
            print(f"worker: could not fetch institution {institution_id}: {exc}", file=sys.stderr)
            continue
        names = [record.legacy_icn or "", *record.name_variants, *record.own_names]
        _insert_aliases(institution_id, [n for n in names if n], "record")
        fetched += 1
    return fetched


def record_enrichment_outcome(name: str, institution_id: str | None, paper_count: int) -> None:
    """Keep unresolved_institutions in step with what enrichment just found.

    A name with no id whose exact-phrase search found nothing is exactly
    the case an alias would fix, so it goes on the worklist. Anything else
    - resolved to an id, or found by name after all - comes off it.
    """
    with get_engine().begin() as conn:
        if institution_id is None and paper_count == 0:
            conn.execute(
                text(
                    """
                    INSERT INTO unresolved_institutions (alias, name)
                    VALUES (normalize_institution_name(:name), :name)
                    ON CONFLICT (alias) DO UPDATE
                    SET seen_count = unresolved_institutions.seen_count + 1,
                        last_seen = now()
                    """
                ),
                {"name": name},
            )
        else:
            conn.execute(
                text(
                    "DELETE FROM unresolved_institutions "
                    "WHERE alias = normalize_institution_name(:name)"
                ),
                {"name": name},
            )


def _print_unresolved() -> None:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT name, seen_count, last_seen::date
                FROM unresolved_institutions
                ORDER BY seen_count DESC, last_seen DESC
                """
            )
        ).all()
    if not rows:
        print("No unresolved institutions.")
        return
    for name, seen_count, last_seen in rows:
        print(f"{seen_count:>4}  {last_seen}  {name}")


def _add_curated(name: str, institution_id: str) -> None:
    # Checked against INSPIRE before it's trusted: a mistyped id would
    # otherwise quietly show one university's papers under another's name.
    record = institution_record(institution_id)
    _insert_aliases(record.institution_id, [name], "curated")
    print(f"{name!r} -> {record.institution_id} ({record.legacy_icn})")


def _remove_curated(name: str, institution_id: str) -> None:
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                """
                DELETE FROM institution_aliases
                WHERE alias = normalize_institution_name(:name)
                  AND institution_id = :institution_id AND source = 'curated'
                """
            ),
            {"name": name, "institution_id": institution_id},
        )
    print(f"removed {result.rowcount} alias(es)")


def main(argv: list[str]) -> int:
    usage = __doc__.split("\n\n")[-1]
    if argv[:1] == ["unresolved"] and len(argv) == 1:
        _print_unresolved()
    elif argv[:1] == ["add"] and len(argv) == 3:
        _add_curated(argv[1], argv[2])
    elif argv[:1] == ["remove"] and len(argv) == 3:
        _remove_curated(argv[1], argv[2])
    else:
        print(usage, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
