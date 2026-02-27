#!/usr/bin/env python3
"""
LLM question tagger — Session 3.

Tags Polymarket market questions with a structured schema and stores the
results in data/market_tags.db for use by the tag blocklist filter that runs
in the live trading loop.

Schema stored per market:
  {
    "asset":             str   # "BTC", "ETH", "macro", "politics", "other"
    "event_type":        str   # "price", "election", "politics", "regulation", "sports", "other"
    "horizon":           str   # "short-term" (<7d), "medium-term" (7-30d), "long-term" (>30d)
    "outcome_type":      str   # "binary_price", "binary_misc", "ranked", "numeric"
    "asymmetry_flag":    bool  # True when one outcome is structurally far more likely
    "info_edge_needed":  str   # "low", "medium", "high"
  }

Usage:
    # Tag all recently active markets:
    python scripts/tag_market_questions.py

    # Force re-tag even if already in DB:
    python scripts/tag_market_questions.py --force

    # Limit to N markets per run (batching):
    python scripts/tag_market_questions.py --limit 50

    # Dry-run: print tags but do NOT write to DB:
    python scripts/tag_market_questions.py --dry-run

Environment:
    OPENAI_API_KEY  — required

WARNING: This script is NEVER called from the hot path.
         It is a manual / scheduled offline tool.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Dependency guard: openai is optional (not guaranteed in base environment)
# ---------------------------------------------------------------------------
try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Path setup (run from repo root or scripts/ directory)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_TAGS_DB_PATH = _REPO_ROOT / "data" / "market_tags.db"
_TRADING_DB_PATH = _REPO_ROOT / "data" / "trading.db"

# ---------------------------------------------------------------------------
# DB initialisation
# ---------------------------------------------------------------------------
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS market_tags (
    market_id        TEXT PRIMARY KEY,
    question         TEXT,
    asset            TEXT,
    event_type       TEXT,
    horizon          TEXT,
    outcome_type     TEXT,
    asymmetry_flag   INTEGER DEFAULT 0,
    info_edge_needed TEXT,
    raw_json         TEXT,
    tagged_at        REAL,
    model            TEXT
);
"""


def _init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Fetch market questions to tag
# ---------------------------------------------------------------------------
def _fetch_untagged_market_ids(limit: int, force: bool) -> list[dict]:
    """
    Returns a list of {market_id, question} dicts from trading.db that are
    either not yet in market_tags.db (default) or all of them (--force).
    """
    if not _TRADING_DB_PATH.exists():
        print(f"[WARN] trading.db not found at {_TRADING_DB_PATH}, nothing to tag.")
        return []

    with sqlite3.connect(str(_TRADING_DB_PATH)) as trade_conn:
        rows = trade_conn.execute(
            "SELECT DISTINCT market_id, notes FROM order_tracking "
            "ORDER BY created_at DESC LIMIT 2000"
        ).fetchall()

    # Parse question from notes field ("question=<value> side=...")
    candidates = []
    seen = set()
    for market_id, notes in rows:
        if market_id in seen:
            continue
        seen.add(market_id)
        question = ""
        if notes and "question=" in notes:
            try:
                question = notes.split("question=")[1].split(" ")[0].strip()
            except Exception:
                pass
        candidates.append({"market_id": market_id, "question": question})

    if force:
        return candidates[:limit]

    # Filter to untagged
    with _init_db(_TAGS_DB_PATH) as tag_conn:
        tagged = {
            row[0]
            for row in tag_conn.execute("SELECT market_id FROM market_tags").fetchall()
        }

    return [c for c in candidates if c["market_id"] not in tagged][:limit]


# ---------------------------------------------------------------------------
# LLM prompting
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """
You are a financial market question classifier.
Given a Polymarket question, output ONLY a JSON object (no explanation) with
these exact keys:

  asset            -- string: one of "BTC", "ETH", "macro", "politics", "sports", "other"
  event_type       -- string: one of "price", "election", "politics", "regulation", "sports", "other"
  horizon          -- string: one of "short-term", "medium-term", "long-term"
                       short-term  = expires in < 7 days
                       medium-term = expires in 7-30 days
                       long-term   = expires in > 30 days
  outcome_type     -- string: one of "binary_price", "binary_misc", "ranked", "numeric"
  asymmetry_flag   -- boolean: true if one outcome is structurally far more likely
                       (e.g. "Will BTC exceed $1M by 2024?" — YES is ~0% likely)
  info_edge_needed -- string: one of "low", "medium", "high"
                       low    = outcome predictable from public data alone
                       medium = some interpretation / timing skill needed
                       high   = requires private information or deep domain expertise

Return only the JSON. Example:
{
  "asset": "BTC",
  "event_type": "price",
  "horizon": "short-term",
  "outcome_type": "binary_price",
  "asymmetry_flag": false,
  "info_edge_needed": "low"
}
""".strip()


def _tag_question_with_llm(
    client: "OpenAI",
    question: str,
    model: str = "gpt-4o-mini",
    max_retries: int = 3,
) -> Optional[dict]:
    """
    Calls OpenAI and returns parsed tag dict, or None on failure.
    Retries up to max_retries times with exponential back-off.
    """
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Question: {question}"},
                ],
                temperature=0.0,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            raw_text = resp.choices[0].message.content or ""
            parsed = json.loads(raw_text)
            # Validate required keys
            required = {"asset", "event_type", "horizon", "outcome_type", "asymmetry_flag", "info_edge_needed"}
            if not required.issubset(parsed.keys()):
                missing = required - parsed.keys()
                print(f"  [WARN] LLM response missing keys: {missing}. Raw: {raw_text[:120]}")
                return None
            return parsed
        except json.JSONDecodeError as e:
            print(f"  [WARN] JSON parse error on attempt {attempt + 1}: {e}")
        except Exception as e:
            print(f"  [WARN] LLM error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------
def _upsert_tag(
    conn: sqlite3.Connection,
    market_id: str,
    question: str,
    tags: dict,
    model: str,
) -> None:
    conn.execute(
        """
        INSERT INTO market_tags
            (market_id, question, asset, event_type, horizon, outcome_type,
             asymmetry_flag, info_edge_needed, raw_json, tagged_at, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            question         = excluded.question,
            asset            = excluded.asset,
            event_type       = excluded.event_type,
            horizon          = excluded.horizon,
            outcome_type     = excluded.outcome_type,
            asymmetry_flag   = excluded.asymmetry_flag,
            info_edge_needed = excluded.info_edge_needed,
            raw_json         = excluded.raw_json,
            tagged_at        = excluded.tagged_at,
            model            = excluded.model
        """,
        (
            market_id,
            question,
            tags.get("asset", "other"),
            tags.get("event_type", "other"),
            tags.get("horizon", "medium-term"),
            tags.get("outcome_type", "binary_misc"),
            1 if tags.get("asymmetry_flag") else 0,
            tags.get("info_edge_needed", "medium"),
            json.dumps(tags),
            time.time(),
            model,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tag Polymarket market questions via LLM and store in market_tags.db"
    )
    parser.add_argument("--limit", type=int, default=100,
                        help="Max markets to process per run (default: 100)")
    parser.add_argument("--force", action="store_true",
                        help="Re-tag markets that already have tags in DB")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print tags but do not write to DB")
    parser.add_argument("--model", default="gpt-4o-mini",
                        help="OpenAI model to use (default: gpt-4o-mini)")
    parser.add_argument("--db", default=str(_TAGS_DB_PATH),
                        help=f"Path to market_tags.db (default: {_TAGS_DB_PATH})")
    args = parser.parse_args()

    if not _OPENAI_AVAILABLE:
        print("ERROR: openai package is not installed.")
        print("  Install with: pip install 'openai>=1.12.0,<2.0.0'")
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY environment variable is not set.")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    db_path = Path(args.db)

    markets = _fetch_untagged_market_ids(args.limit, args.force)
    if not markets:
        print("No markets to tag (all already tagged or trading.db is empty).")
        print("  Use --force to re-tag existing records.")
        return

    print(f"Tagging {len(markets)} market(s) using {args.model} …")

    tagged_count = 0
    failed_count = 0

    with _init_db(db_path) as conn:
        for i, rec in enumerate(markets, start=1):
            market_id = rec["market_id"]
            question = rec["question"] or market_id
            print(f"  [{i:3d}/{len(markets)}] {market_id[:20]:<20}  {question[:60]}")

            tags = _tag_question_with_llm(client, question, model=args.model)
            if tags is None:
                print(f"            => FAILED — skipping")
                failed_count += 1
                continue

            print(
                f"            => {tags.get('asset')}/{tags.get('event_type')}/"
                f"{tags.get('horizon')}/{tags.get('outcome_type')}  "
                f"edge={tags.get('info_edge_needed')}  "
                f"asym={tags.get('asymmetry_flag')}"
            )

            if not args.dry_run:
                _upsert_tag(conn, market_id, question, tags, args.model)

            tagged_count += 1
            # Polite rate-limiting: ~30 RPM easily within free-tier gpt-4o-mini limits
            time.sleep(0.1)

    verb = "Would tag" if args.dry_run else "Tagged"
    print(f"\n{verb} {tagged_count} market(s).  {failed_count} failed.")
    if args.dry_run:
        print("  (dry-run: nothing written to DB)")
    else:
        print(f"  DB: {db_path}")
        print(f"  Refresh live cache: restart bot or wait 5 min (TTL-based refresh).")


if __name__ == "__main__":
    main()
