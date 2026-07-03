"""
Migration script: SQLite → PostgreSQL.

Run once before dropping the old SQLite file:
    python migrate.py

Logs row counts per table. Safe to re-run (ON CONFLICT DO NOTHING).
"""

import json
import sqlite3
from pathlib import Path

import psycopg2
import psycopg2.extras

from config import DATABASE_URL
from logger import get_logger

log = get_logger("migrate")

SQLITE_PATH = Path(__file__).parent / "data" / "tenders.db"

REGIONS = ["Минская", "Витебская", "Могилёвская", "Гродненская",
           "Брестская", "Гомельская", "г. Минск"]


def _extract_region(text: str | None) -> str | None:
    if not text:
        return None
    for r in REGIONS:
        if r.lower() in text.lower():
            return r
    return None


def _map_verdict_to_status(verdict: str | None) -> str:
    if verdict == "🟢":
        return "suitable"
    if verdict == "🔴":
        return "rejected"
    return "pending"


def migrate():
    if not SQLITE_PATH.exists():
        log.info(f"SQLite DB not found at {SQLITE_PATH} — nothing to migrate")
        return

    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(DATABASE_URL)
    pg_conn.autocommit = False
    pg_cur = pg_conn.cursor()

    # ------------------------------------------------------------------
    # price_items
    # ------------------------------------------------------------------
    old_items = sqlite_conn.execute("SELECT * FROM price_items").fetchall()
    migrated_items = 0
    for row in old_items:
        pg_cur.execute("""
            INSERT INTO price_items
                (name, category, unit, my_price, is_active, is_unknown, occurrences, created_at, updated_at)
            VALUES (%s, %s, %s, %s, TRUE, FALSE, %s, NOW(), %s)
            ON CONFLICT DO NOTHING
        """, (
            row["name"],
            row["category"] or "прочее",
            row["unit"] or "",
            row["my_price"],
            row["occurrences"] or 0,
            row["updated_at"] or "NOW()",
        ))
        if pg_cur.rowcount:
            migrated_items += 1

    log.info(f"price_items migrated: {migrated_items}/{len(old_items)}")

    # ------------------------------------------------------------------
    # tenders (old schema → new schema)
    # ------------------------------------------------------------------
    try:
        old_tenders = sqlite_conn.execute("SELECT * FROM tenders").fetchall()
    except sqlite3.OperationalError:
        old_tenders = []

    migrated_tenders = 0
    for row in old_tenders:
        d = dict(row)

        title = d.get("object_name") or "Без названия"
        budget = d.get("budget_limit") or d.get("total_estimate")
        region = _extract_region(d.get("address"))
        status = _map_verdict_to_status(d.get("verdict"))
        m_score = (d.get("margin_pct") or 0) / 100.0
        external_id = f"old_{d['id']}"
        created_at = d.get("uploaded_at") or "NOW()"

        pg_cur.execute("""
            INSERT INTO tenders
                (external_id, source, title, url, region, budget_byn, deadline,
                 status, m_score, created_at, updated_at)
            VALUES (%s, 'legacy', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (external_id, source) DO NOTHING
        """, (
            external_id, title, None, region, budget,
            d.get("deadline"), status, m_score, created_at, created_at,
        ))
        if pg_cur.rowcount:
            migrated_tenders += 1

    log.info(f"tenders migrated: {migrated_tenders}/{len(old_tenders)}")

    pg_conn.commit()
    pg_conn.close()
    sqlite_conn.close()
    log.info("Migration complete.")


if __name__ == "__main__":
    migrate()
