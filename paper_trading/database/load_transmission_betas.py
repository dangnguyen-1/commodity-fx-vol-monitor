"""Loads measured transmission betas into the live instrument registry.

The spec assumed a standardized commodity move produces an equally large
standardized FX move -- `relationship_direction` of +1 or -1, applied as a
magnitude. Measured at the trading horizon that is wrong for every
relationship: the median is 0.244 and the strongest reaches 0.664, so the
expected impulse, and every divergence score built from it, has been
inflated several-fold.

This writes the measured coefficient per relationship. It reads a CSV rather
than embedding numbers in code, so the values are auditable, diffable, and
regenerable from `strategy/research/intraday_beta.py` when more history has
accumulated.

CSV columns: relationship_id, transmission_beta, observations

Relationships absent from the file keep a NULL beta. The feature engine
refuses to build an expected impulse for those rather than falling back to
1, because 1 is precisely the value now known to be wrong -- a silent
fallback would preserve the bug for exactly the relationships nobody has
checked.

Usage:
    python3 -m paper_trading.database.load_transmission_betas \\
        --source strategy/research/results/transmission_betas.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "paper_trading" / "data" / "paper_trading.db"
)
DEFAULT_SOURCE = (
    PROJECT_ROOT
    / "strategy"
    / "research"
    / "results"
    / "transmission_betas.csv"
)

# Below this the estimate is not worth acting on. Cattle produced a beta of
# 0.96 from 40 observations during the first measurement run, which would
# have been the largest coefficient in the book on the strength of noise.
MINIMUM_OBSERVATIONS = 200


def load_betas(
    *,
    database_path: Path,
    source: Path,
) -> dict[str, int]:
    with source.open(encoding="utf-8") as handle:
        records = list(csv.DictReader(handle))

    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    skipped_thin = 0
    unknown = 0

    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        known = {
            str(row[0])
            for row in connection.execute(
                "SELECT relationship_id FROM live_instrument_registry"
            )
        }

        for record in records:
            relationship_id = record["relationship_id"].strip()
            if relationship_id not in known:
                unknown += 1
                continue

            observations = int(record.get("observations") or 0)
            if observations < MINIMUM_OBSERVATIONS:
                skipped_thin += 1
                continue

            connection.execute(
                """
                UPDATE live_instrument_registry
                SET transmission_beta = ?,
                    transmission_beta_observations = ?,
                    transmission_beta_measured_at_utc = ?,
                    updated_at_utc = ?
                WHERE relationship_id = ?
                """,
                (
                    float(record["transmission_beta"]),
                    observations,
                    now,
                    now,
                    relationship_id,
                ),
            )
            applied += 1

        connection.commit()

        measured = connection.execute(
            """
            SELECT COUNT(*) FROM live_instrument_registry
            WHERE active = 1 AND transmission_beta IS NOT NULL
            """
        ).fetchone()[0]
        active = connection.execute(
            "SELECT COUNT(*) FROM live_instrument_registry WHERE active = 1"
        ).fetchone()[0]
    finally:
        connection.close()

    return {
        "applied": applied,
        "skipped_thin": skipped_thin,
        "unknown": unknown,
        "active_measured": measured,
        "active_total": active,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load measured transmission betas into the registry."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"No beta measurements at {args.source}")

    result = load_betas(
        database_path=args.database, source=args.source
    )

    print("Transmission betas loaded.")
    print(f"  applied:            {result['applied']}")
    print(
        f"  skipped (< {MINIMUM_OBSERVATIONS} obs): "
        f"{result['skipped_thin']}"
    )
    print(f"  unknown relationship: {result['unknown']}")
    print(
        f"  active relationships with a measured beta: "
        f"{result['active_measured']}/{result['active_total']}"
    )
    if result["active_measured"] < result["active_total"]:
        print()
        print(
            "  Relationships without a measured beta will not produce an "
            "expected impulse. That is deliberate -- falling back to 1 "
            "would keep the very error this replaces."
        )


if __name__ == "__main__":
    main()
