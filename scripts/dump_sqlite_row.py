#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dump one SQLite row as JSON.")
    parser.add_argument("--db-path", required=True, help="SQLite database path")
    parser.add_argument(
        "--table",
        required=True,
        choices=["market_quarantine", "calibration_observations"],
        help="Table to query",
    )
    parser.add_argument("--where", default=None, help="Optional SQL WHERE clause without the WHERE keyword")
    parser.add_argument("--order-by", default=None, help="Optional ORDER BY clause without the ORDER BY keyword")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db_path)
    query = f"SELECT * FROM {args.table}"
    if args.where:
        query += f" WHERE {args.where}"
    if args.order_by:
        query += f" ORDER BY {args.order_by}"
    query += " LIMIT 1"

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(query).fetchone()
        payload = {
            "db_path": str(db_path),
            "table": args.table,
            "row": dict(row) if row else None,
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    finally:
        connection.close()


if __name__ == "__main__":
    main()