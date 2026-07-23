"""PostgreSQL database layer — all DB access goes through this module."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config import DATABASE_URL
from logger import get_logger

log = get_logger("db")

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, DATABASE_URL)
    return _pool


@contextmanager
def _conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db():
    with _conn() as conn:
        cur = conn.cursor()

        # price_items must come before tender_positions (FK reference)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_items (
                id          SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                category    TEXT,
                unit        TEXT,
                my_price    FLOAT,
                is_active   BOOLEAN DEFAULT TRUE,
                is_unknown  BOOLEAN DEFAULT FALSE,
                occurrences INT DEFAULT 0,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenders (
                id            SERIAL PRIMARY KEY,
                external_id   TEXT NOT NULL,
                source        TEXT NOT NULL,
                title         TEXT,
                url           TEXT,
                region        TEXT,
                budget_byn    FLOAT,
                deadline      TEXT,
                status        TEXT DEFAULT 'pending',
                reject_reason TEXT,
                k_score       FLOAT,
                l_score       FLOAT,
                m_score       FLOAT,
                s_score       FLOAT,
                ai_comment    TEXT,
                last_status_check TIMESTAMPTZ,
                created_at    TIMESTAMPTZ DEFAULT NOW(),
                updated_at    TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(external_id, source)
            )
        """)

        # Migration for pre-existing installations: CREATE TABLE IF NOT
        # EXISTS above only takes effect on a fresh table, so an already-
        # deployed `tenders` table needs this column added explicitly.
        cur.execute("""
            ALTER TABLE tenders ADD COLUMN IF NOT EXISTS last_status_check TIMESTAMPTZ
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tender_positions (
                id                   SERIAL PRIMARY KEY,
                tender_id            INTEGER REFERENCES tenders(id) ON DELETE CASCADE,
                smeta_name           TEXT,
                smeta_unit           TEXT,
                smeta_quantity       FLOAT,
                smeta_labor_cost     FLOAT,
                smeta_material_cost  FLOAT,
                smeta_transport_cost FLOAT,
                smeta_total_cost     FLOAT,
                category             TEXT,
                matched_item_id      INTEGER REFERENCES price_items(id),
                confidence           FLOAT,
                match_status         TEXT,
                margin_byn           FLOAT,
                created_at           TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS search_settings (
                id    SERIAL PRIMARY KEY,
                key   TEXT UNIQUE NOT NULL,
                value TEXT
            )
        """)

        # Everything the VPS scraper sees (minus marketing-type/inactive-lot
        # placeholders it already drops) lands here first, unfiltered by
        # budget/region. B/R are computed live against search_settings —
        # see classify_raw_budget_region() — so loosening a threshold in the
        # UI can recover previously-filtered rows without re-scraping.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenders_raw (
                id            SERIAL PRIMARY KEY,
                external_id   TEXT NOT NULL,
                source        TEXT NOT NULL,
                title         TEXT,
                url           TEXT,
                region        TEXT,
                budget_byn    FLOAT,
                deadline      TEXT,
                tender_type   TEXT,
                status        TEXT DEFAULT 'raw',
                reject_reason TEXT,
                created_at    TIMESTAMPTZ DEFAULT NOW(),
                updated_at    TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(external_id, source)
            )
        """)

        # Singleton row (id=1) tracking the freshness-revalidation job: lets
        # a dashboard button request an on-demand run and poll for it to
        # finish, without a full job-queue system — the VPS's manual-trigger
        # poll job checks manual_requested_at, and any run that finishes
        # AFTER that timestamp (scheduled or manual) satisfies the request.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS revalidation_state (
                id                   INT PRIMARY KEY DEFAULT 1,
                manual_requested_at  TIMESTAMPTZ,
                last_run_started_at  TIMESTAMPTZ,
                last_run_finished_at TIMESTAMPTZ,
                last_run_trigger     TEXT,
                last_checked_count   INT,
                last_archived_count  INT,
                CHECK (id = 1)
            )
        """)
        cur.execute("""
            INSERT INTO revalidation_state (id) VALUES (1)
            ON CONFLICT (id) DO NOTHING
        """)

        # Company requisites for document generation. SENSITIVE: contains
        # УНП and bank details — never expose through public API responses,
        # only through the dashboard profile tab and generated documents.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS company_profile (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Seed default settings (skip if already exist)
        defaults = [
            ("min_budget", "36000"),
            # Separate floor for goszakupki single-source purchases (materials,
            # small works are often 500-30000 BYN) — see classify_raw_budget_region().
            # Defaults equal to min_budget, i.e. behavior does not change until
            # the contractor consciously lowers it in the settings UI.
            ("min_budget_single_source", "36000"),
            ("x_threshold", "30"),
            ("y_threshold", "5"),
            ("regions", json.dumps(["Минская", "г. Минск"])),
        ]
        for key, value in defaults:
            cur.execute("""
                INSERT INTO search_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING
            """, (key, value))

    log.info("Database initialized")


# ---------------------------------------------------------------------------
# Tenders
# ---------------------------------------------------------------------------

def tender_exists(external_id: str, source: str) -> bool:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT 1 FROM tenders WHERE external_id = %s AND source = %s",
            (external_id, source),
        )
        return cur.fetchone() is not None


def delete_failed_tender(external_id: str, source: str) -> bool:
    """Remove a tender whose ANALYSIS failed (ai_error) so a re-ingest can
    retry it — transient AI failures must not permanently blacklist a
    tender. Formula-rejected and suitable tenders are never touched.
    Positions cascade-delete. Returns True when a row was removed."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM tenders
            WHERE external_id = %s AND source = %s
              AND status = 'rejected' AND reject_reason = 'ai_error'
        """, (external_id, source))
        return cur.rowcount > 0


def save_tender(tender: dict) -> int:
    """Insert new tender with status=pending. Returns tender id."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tenders
                (external_id, source, title, url, region, budget_byn, deadline, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id
        """, (
            tender["external_id"],
            tender["source"],
            tender.get("title"),
            tender.get("url"),
            tender.get("region"),
            tender.get("budget_byn"),
            tender.get("deadline"),
        ))
        return cur.fetchone()[0]


def reject_tender(tender_id: int, reason: str):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders
            SET status = 'rejected', reject_reason = %s, updated_at = NOW()
            WHERE id = %s
        """, (reason, tender_id))


def update_tender_scores(tender_id: int, k: float, l: float, m: float, s: float):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders
            SET k_score = %s, l_score = %s, m_score = %s, s_score = %s, updated_at = NOW()
            WHERE id = %s
        """, (k, l, m, s, tender_id))


def update_tender_status(tender_id: int, status: str):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders SET status = %s, updated_at = NOW() WHERE id = %s
        """, (status, tender_id))


def update_tender_ai_comment(tender_id: int, comment: str):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders SET ai_comment = %s, updated_at = NOW() WHERE id = %s
        """, (comment, tender_id))


def get_tender(tender_id: int) -> dict | None:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM tenders WHERE id = %s", (tender_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_last_external_id(source: str) -> str | None:
    """Return the external_id of the most recently saved tender for this source."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT external_id FROM tenders
            WHERE source = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (source,))
        row = cur.fetchone()
        return row[0] if row else None


# Rejection reasons that a settings change can overturn — everything the
# Python filters/formula decided. ai_error records are NOT rescorable (the
# analysis itself failed, there are no stored positions to re-score).
_RESCORABLE_REASONS = ("failed_B", "failed_R", "failed_K", "failed_L", "failed_M")


def get_rescorable_tenders() -> list[dict]:
    """Tenders whose status was decided by the search formula and therefore
    must be recomputed when the user changes parameters."""
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT * FROM tenders
            WHERE status = 'suitable'
               OR (status = 'rejected' AND reject_reason IN %s)
        """, (_RESCORABLE_REASONS,))
        return [dict(r) for r in cur.fetchall()]


def archive_tender(tender_id: int, marker: str):
    """Park a tender that can't be re-scored under new settings; the marker
    (e.g. 'результаты до изменения от 11.07.2026') is shown instead of mixing
    old-criteria results with new ones."""
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders
            SET status = 'archived', reject_reason = %s, updated_at = NOW()
            WHERE id = %s
        """, (marker, tender_id))


def get_suitable_tenders(limit: int = 200) -> list[dict]:
    # 'submitted' stays visible: the user marked those manually and still
    # wants to see them among the suitable ones (with a badge). 'archived'
    # is included too (greyed out client-side) — these were suitable/
    # submitted until revalidation found the source site closed them; the
    # contractor should see that a tender disappeared and why, not have it
    # silently vanish. Active tenders always sort before archived ones.
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT * FROM tenders
            WHERE status IN ('suitable', 'submitted', 'archived')
            ORDER BY (status = 'archived') ASC, s_score DESC NULLS LAST, created_at DESC
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_rejected_counts(hours: int = 24) -> dict:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT reject_reason, COUNT(*) as cnt
            FROM tenders
            WHERE status = 'rejected'
              AND created_at >= NOW() - INTERVAL '%s hours'
            GROUP BY reject_reason
        """ % hours)
        return {row["reject_reason"]: row["cnt"] for row in cur.fetchall()}


def get_last_suitable_tenders(limit: int = 5) -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT id, title, budget_byn, region, k_score, l_score, m_score, s_score
            FROM tenders
            WHERE status = 'suitable'
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def count_tenders() -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tenders")
        return cur.fetchone()[0]


def count_suitable() -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tenders WHERE status = 'suitable'")
        return cur.fetchone()[0]


def count_rejected(reason: str) -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM tenders WHERE status = 'rejected' AND reject_reason = %s",
            (reason,),
        )
        return cur.fetchone()[0]


def avg_margin() -> float:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT AVG(m_score) FROM tenders WHERE status = 'suitable' AND m_score IS NOT NULL")
        val = cur.fetchone()[0]
        return round(float(val) * 100, 1) if val else 0.0


# ---------------------------------------------------------------------------
# Tender positions
# ---------------------------------------------------------------------------

def save_tender_positions(tender_id: int, positions: list[dict]):
    """Insert extracted + matched positions. Each dict has smeta_* and match fields."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tender_positions WHERE tender_id = %s", (tender_id,))
        for p in positions:
            cur.execute("""
                INSERT INTO tender_positions
                    (tender_id, smeta_name, smeta_unit, smeta_quantity,
                     smeta_labor_cost, smeta_material_cost, smeta_transport_cost,
                     smeta_total_cost, category, matched_item_id, confidence,
                     match_status, margin_byn)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                tender_id,
                p.get("smeta_name"),
                p.get("smeta_unit"),
                p.get("smeta_quantity"),
                p.get("smeta_labor_cost"),
                p.get("smeta_material_cost"),
                p.get("smeta_transport_cost"),
                p.get("smeta_total_cost"),
                p.get("category"),
                p.get("matched_item_id"),
                p.get("confidence"),
                p.get("match_status"),
                p.get("margin_byn"),
            ))


def get_tender_positions(tender_id: int) -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT
                tp.*,
                pi.name  AS matched_item_name,
                pi.my_price AS matched_my_price
            FROM tender_positions tp
            LEFT JOIN price_items pi ON tp.matched_item_id = pi.id
            WHERE tp.tender_id = %s
            ORDER BY tp.smeta_total_cost DESC NULLS LAST
        """, (tender_id,))
        return [dict(r) for r in cur.fetchall()]


def get_top_positions(tender_id: int, limit: int = 5) -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT smeta_name, smeta_unit, smeta_quantity, smeta_total_cost, match_status
            FROM tender_positions
            WHERE tender_id = %s
            ORDER BY smeta_total_cost DESC NULLS LAST
            LIMIT %s
        """, (tender_id, limit))
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Price items
# ---------------------------------------------------------------------------

def get_active_price_item_names() -> list[str]:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM price_items WHERE is_active = TRUE")
        return [r[0] for r in cur.fetchall()]


def get_active_price_items() -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT * FROM price_items WHERE is_active = TRUE ORDER BY category, name
        """)
        return [dict(r) for r in cur.fetchall()]


def get_all_price_items() -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM price_items ORDER BY category, name")
        return [dict(r) for r in cur.fetchall()]


def get_price_item(item_id: int) -> dict | None:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM price_items WHERE id = %s", (item_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def price_items_count() -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM price_items WHERE is_unknown = FALSE")
        return cur.fetchone()[0]


def save_price_item(item_id: int | None, name: str, unit: str,
                    my_price: float, category: str, is_active: bool = True) -> int:
    with _conn() as conn:
        cur = conn.cursor()
        if item_id:
            cur.execute("""
                UPDATE price_items
                SET name=%s, unit=%s, my_price=%s, category=%s, is_active=%s, updated_at=NOW()
                WHERE id=%s
                RETURNING id
            """, (name, unit, my_price, category, is_active, item_id))
            return cur.fetchone()[0]
        else:
            cur.execute("""
                INSERT INTO price_items (name, unit, my_price, category, is_active)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (name, unit, my_price, category, is_active))
            return cur.fetchone()[0]


def toggle_price_item(item_id: int, is_active: bool):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE price_items SET is_active = %s, updated_at = NOW() WHERE id = %s
        """, (is_active, item_id))


def delete_price_item(item_id: int):
    with _conn() as conn:
        conn.cursor().execute("DELETE FROM price_items WHERE id = %s", (item_id,))


def add_unknown_price_item(name: str, unit: str, my_price: float):
    """Add grey-match position as inactive unknown item (contractor reviews later)."""
    with _conn() as conn:
        cur = conn.cursor()
        # Skip if already exists (by name)
        cur.execute("SELECT 1 FROM price_items WHERE name = %s", (name,))
        if cur.fetchone():
            return
        cur.execute("""
            INSERT INTO price_items (name, unit, my_price, is_active, is_unknown)
            VALUES (%s, %s, %s, FALSE, TRUE)
        """, (name, unit, my_price))


def insert_price_items_batch(items: list[dict]):
    """Bulk insert for initial price list population."""
    with _conn() as conn:
        cur = conn.cursor()
        for item in items:
            cur.execute("""
                INSERT INTO price_items (name, category, unit, my_price, is_active, is_unknown)
                VALUES (%s, %s, %s, %s, TRUE, FALSE)
                ON CONFLICT DO NOTHING
            """, (item["name"], item["category"], item["unit"], item["my_price"]))


# ---------------------------------------------------------------------------
# Search settings
# ---------------------------------------------------------------------------

def get_search_settings() -> dict:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT key, value FROM search_settings")
        rows = {r["key"]: r["value"] for r in cur.fetchall()}
    return {
        "min_budget": float(rows.get("min_budget", 36000)),
        "min_budget_single_source": float(
            rows.get("min_budget_single_source", rows.get("min_budget", 36000))
        ),
        "x_threshold": float(rows.get("x_threshold", 30)),
        "y_threshold": float(rows.get("y_threshold", 5)),
        "regions": json.loads(rows.get("regions", '["Минская","г. Минск"]')),
        # When the parameters were last changed (ISO); None until first save.
        # The dashboard shows results as "актуально на {this date}".
        "params_updated_at": rows.get("params_updated_at"),
    }


def update_search_settings(settings: dict):
    # Every save stamps the change moment so the dashboard can state which
    # parameter version the displayed results were computed under.
    settings = {**settings, "params_updated_at": _now()}
    with _conn() as conn:
        cur = conn.cursor()
        for key, value in settings.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            else:
                value = str(value)
            cur.execute("""
                INSERT INTO search_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, value))


# ---------------------------------------------------------------------------
# Raw tenders (everything the VPS scrapes, pre-B/R — see /api/ingest_raw)
# ---------------------------------------------------------------------------

def save_raw_tender(tender: dict) -> int:
    """Upsert by (external_id, source). Does not touch status/reject_reason —
    the caller classifies separately (see classify_raw_budget_region), since
    an update here can arrive from a fresh scrape of a row already classified."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tenders_raw
                (external_id, source, title, url, region, budget_byn, deadline, tender_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (external_id, source) DO UPDATE SET
                title = EXCLUDED.title,
                url = EXCLUDED.url,
                region = EXCLUDED.region,
                budget_byn = EXCLUDED.budget_byn,
                deadline = EXCLUDED.deadline,
                tender_type = EXCLUDED.tender_type,
                updated_at = NOW()
            RETURNING id
        """, (
            tender["external_id"],
            tender["source"],
            tender.get("title"),
            tender.get("url"),
            tender.get("region"),
            tender.get("budget_byn"),
            tender.get("deadline"),
            tender.get("tender_type"),
        ))
        return cur.fetchone()[0]


def get_all_raw_tenders() -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM tenders_raw ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]


def get_raw_by_status(status: str) -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT * FROM tenders_raw WHERE status = %s ORDER BY created_at DESC",
            (status,),
        )
        return [dict(r) for r in cur.fetchall()]


def update_raw_status(raw_id: int, status: str, reject_reason: str | None = None):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders_raw
            SET status = %s, reject_reason = %s, updated_at = NOW()
            WHERE id = %s
        """, (status, reject_reason, raw_id))


def classify_raw_budget_region(row: dict, settings: dict) -> tuple[str, str | None]:
    """B/R against CURRENT search_settings — shared by /api/ingest_raw (one
    row, right after scraping) and recompute_all_raw_BR (every row, right
    after a settings save). Budget floor is per procedure type: goszakupki
    single-source purchases (materials, small works) get their own,
    typically lower, floor — see min_budget_single_source in get_search_settings."""
    budget = row.get("budget_byn") or 0
    min_budget = (
        settings.get("min_budget_single_source")
        if row.get("tender_type") == "single-source"
        else settings.get("min_budget")
    )
    if budget < float(min_budget or 0):
        return "filtered_out", "budget"

    regions = settings.get("regions") or []
    if regions and row.get("region") not in regions:
        return "filtered_out", "region"

    return "passed", None


def recompute_all_raw_BR(settings: dict) -> dict:
    """Re-classify every tenders_raw row against the settings just saved —
    run this on every settings save so a loosened threshold instantly
    recovers previously filtered_out rows, with no re-scraping."""
    stats = {"passed": 0, "filtered_out": 0}
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT id, budget_byn, region, tender_type FROM tenders_raw")
        rows = [dict(r) for r in cur.fetchall()]

        cur2 = conn.cursor()
        for row in rows:
            status, reason = classify_raw_budget_region(row, settings)
            stats[status] += 1
            cur2.execute("""
                UPDATE tenders_raw
                SET status = %s, reject_reason = %s, updated_at = NOW()
                WHERE id = %s
            """, (status, reason, row["id"]))

    log.info(f"recompute_all_raw_BR: {stats}")
    return stats


def get_pending_document_fetches(source: str) -> list[dict]:
    """Raw tenders that passed B/R but have no matching row yet in `tenders`
    (i.e. documents were never fetched/analyzed) — polled by the VPS."""
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT r.external_id, r.source, r.title, r.url, r.region,
                   r.budget_byn, r.deadline, r.tender_type
            FROM tenders_raw r
            WHERE r.status = 'passed' AND r.source = %s
              AND NOT EXISTS (
                  SELECT 1 FROM tenders t
                  WHERE t.external_id = r.external_id AND t.source = r.source
              )
            ORDER BY r.created_at ASC
        """, (source,))
        return [dict(r) for r in cur.fetchall()]


def get_all_tenders_combined() -> list[dict]:
    """Full-funnel view for the 'Все тендеры' dashboard page: every
    tenders_raw row that never made it to `tenders` (filtered_out by B/R,
    or passed but not yet fetched/analyzed) UNIONed with every `tenders`
    row (which carries scores once analyzed). tender_type only lives on
    tenders_raw, so analyzed rows pull it via a LEFT JOIN on
    (external_id, source) rather than adding a column to `tenders`.

    Each row gets two derived flags the template/JS need to get sorting
    and filtering right without ever treating a missing score as zero:
      - has_score: True only for rows with a real s_score (suitable/
        submitted/archived-that-was-suitable). Rejected tenders (any
        reason) never reach the scoring step, so they're always None here.
      - is_queued: True only for rows genuinely awaiting processing
        (tenders_raw 'passed'/'raw' with no tenders row yet, or a
        `tenders` row stuck at 'pending' mid-analysis) — distinct from
        a rejected tender that also happens to have no score.
    """
    with _conn() as conn:
        cur = _dict_cursor(conn)

        cur.execute("""
            SELECT
                t.id, t.external_id, t.source, t.title, t.url, t.region,
                t.budget_byn, t.deadline, t.status, t.reject_reason,
                t.k_score, t.l_score, t.m_score, t.s_score,
                t.last_status_check, t.created_at,
                r.tender_type
            FROM tenders t
            LEFT JOIN tenders_raw r
                ON r.external_id = t.external_id AND r.source = t.source
        """)
        analyzed = [dict(row) for row in cur.fetchall()]

        cur.execute("""
            SELECT
                r.id, r.external_id, r.source, r.title, r.url, r.region,
                r.budget_byn, r.deadline, r.status, r.reject_reason,
                r.tender_type, r.created_at
            FROM tenders_raw r
            WHERE NOT EXISTS (
                SELECT 1 FROM tenders t
                WHERE t.external_id = r.external_id AND t.source = r.source
            )
        """)
        raw_only = [dict(row) for row in cur.fetchall()]

    def _display_status(status: str, is_queued: bool) -> str:
        # Single value the template branches on for badge color/text —
        # 'queued' always wins regardless of the underlying status string
        # ('pending' in tenders, 'passed'/'raw' in tenders_raw).
        if is_queued:
            return "queued"
        if status in ("suitable", "submitted"):
            return "suitable"
        if status == "archived":
            return "archived"
        if status in ("rejected", "filtered_out"):
            return "rejected"
        return status

    rows = []
    for t in analyzed:
        is_queued = t["status"] == "pending"
        rows.append({
            "kind": "tenders",
            "display_status": _display_status(t["status"], is_queued),
            "id": t["id"],
            "external_id": t["external_id"],
            "source": t["source"],
            "title": t["title"],
            "url": t["url"],
            "tender_type": t["tender_type"],
            "region": t["region"],
            "budget_byn": t["budget_byn"],
            "deadline": t["deadline"],
            "status": t["status"],
            "reject_reason": t["reject_reason"],
            "k_score": t["k_score"],
            "l_score": t["l_score"],
            "m_score": t["m_score"],
            "s_score": t["s_score"],
            "last_status_check": t["last_status_check"],
            "created_at": t["created_at"],
            "is_queued": is_queued,
            "has_score": t["s_score"] is not None,
        })
    for r in raw_only:
        is_queued = r["status"] in ("passed", "raw")
        rows.append({
            "kind": "tenders_raw",
            "display_status": _display_status(r["status"], is_queued),
            "id": r["id"],
            "external_id": r["external_id"],
            "source": r["source"],
            "title": r["title"],
            "url": r["url"],
            "tender_type": r["tender_type"],
            "region": r["region"],
            "budget_byn": r["budget_byn"],
            "deadline": r["deadline"],
            "status": r["status"],
            "reject_reason": r["reject_reason"],
            "k_score": None, "l_score": None, "m_score": None, "s_score": None,
            "last_status_check": None,
            "created_at": r["created_at"],
            "is_queued": is_queued,
            "has_score": False,
        })

    rows.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Tender freshness revalidation — a tender's active/closed status on the
# source site can change after we've already stored it (and possibly shown
# it to the contractor as 'suitable'); this periodically re-checks it.
# ---------------------------------------------------------------------------

REVALIDATE_SAFETY_CAP = 100


def get_tenders_to_revalidate(limit: int = REVALIDATE_SAFETY_CAP) -> list[dict]:
    """ALL still-actionable tenders (suitable/submitted) — unlike an earlier
    version of this function, deadline no longer filters rows out (a closed
    lot is worth knowing about even past its own deadline); it only orders
    them, soonest first, so a capped batch prioritizes the ones that matter
    most. The cap is a safety net now that a daily scheduled run (plus an
    on-demand manual trigger) keeps the backlog from growing large between
    checks — logs a warning if it's ever actually hit, since that would mean
    usage patterns changed enough to reconsider it."""
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM tenders
            WHERE status IN ('suitable', 'submitted')
              AND url IS NOT NULL AND url != ''
        """)
        total = cur.fetchone()["cnt"]
        if total > limit:
            log.warning(
                f"get_tenders_to_revalidate: {total} eligible tenders exceeds "
                f"the safety cap ({limit}) — serving the {limit} with the "
                f"soonest deadlines this round, rest deferred to next run"
            )

        cur.execute("""
            SELECT external_id, source, url
            FROM tenders
            WHERE status IN ('suitable', 'submitted')
              AND url IS NOT NULL AND url != ''
            ORDER BY deadline ASC NULLS LAST
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def update_tender_freshness(external_id: str, source: str, still_active: bool,
                             checked_at: str | None = None):
    """Applies one revalidation result. still_active=False flips the tender
    to archived (the source site no longer shows it as open) instead of
    silently deleting it, so the contractor can see it was pulled and why.
    last_status_check is stamped either way — that's the point of this
    function even when nothing else changes."""
    with _conn() as conn:
        cur = conn.cursor()
        if still_active:
            cur.execute("""
                UPDATE tenders
                SET last_status_check = COALESCE(%s, NOW())
                WHERE external_id = %s AND source = %s
            """, (checked_at, external_id, source))
        else:
            cur.execute("""
                UPDATE tenders
                SET status = 'archived',
                    reject_reason = 'status_changed_after_ingest',
                    last_status_check = COALESCE(%s, NOW()),
                    updated_at = NOW()
                WHERE external_id = %s AND source = %s
            """, (checked_at, external_id, source))


# ---------------------------------------------------------------------------
# Revalidation trigger/status — backs the manual "Проверить актуальность"
# button. No job queue: a run (scheduled OR manual) that finishes after
# manual_requested_at satisfies whatever manual request was pending, since
# both trigger types check the exact same tender set.
# ---------------------------------------------------------------------------

def request_manual_revalidation() -> str:
    """Records a manual trigger request. Returns the requested_at timestamp
    (ISO) so the caller (dashboard JS) can later ask 'is the run that
    satisfies THIS click done yet' rather than being confused by an
    unrelated previous run's completion."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE revalidation_state SET manual_requested_at = NOW() WHERE id = 1
            RETURNING manual_requested_at
        """)
        return cur.fetchone()[0].isoformat()


def get_revalidation_state() -> dict:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM revalidation_state WHERE id = 1")
        row = cur.fetchone()
        return dict(row) if row else {}


def get_pending_manual_revalidation() -> bool:
    """True when a manual request hasn't yet been satisfied by any run
    (scheduled or manual) finishing after it was made."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT manual_requested_at IS NOT NULL
                   AND (last_run_finished_at IS NULL OR last_run_finished_at < manual_requested_at)
            FROM revalidation_state WHERE id = 1
        """)
        row = cur.fetchone()
        return bool(row[0]) if row else False


def mark_revalidation_result(trigger: str, checked_count: int, archived_count: int,
                              started_at: str | None = None, finished_at: str | None = None):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE revalidation_state
            SET last_run_trigger = %s,
                last_run_started_at = COALESCE(%s, last_run_started_at),
                last_run_finished_at = COALESCE(%s, NOW()),
                last_checked_count = %s,
                last_archived_count = %s
            WHERE id = 1
        """, (trigger, started_at, finished_at, checked_count, archived_count))


# ---------------------------------------------------------------------------
# Company profile (sensitive — dashboard/docgen use only)
# ---------------------------------------------------------------------------

COMPANY_PROFILE_FIELDS = [
    "company_name", "unp", "address", "director",
    "phone", "email", "bank_name", "bank_account", "bank_code",
]


def get_company_profile() -> dict:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT key, value FROM company_profile")
        rows = {r["key"]: r["value"] for r in cur.fetchall()}
    return {f: rows.get(f) for f in COMPANY_PROFILE_FIELDS}


def update_company_profile(profile: dict):
    with _conn() as conn:
        cur = conn.cursor()
        for key in COMPANY_PROFILE_FIELDS:
            if key not in profile:
                continue
            cur.execute("""
                INSERT INTO company_profile (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, (profile[key] or "").strip()))


def mark_tender_submitted(tender_id: int):
    """Records the USER's explicit decision — never called automatically
    (Iron rule: submission itself happens manually with the director's
    digital signature on the official portal)."""
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders SET status = 'submitted', updated_at = NOW()
            WHERE id = %s AND status = 'suitable'
        """, (tender_id,))


# ---------------------------------------------------------------------------
# AI assistant context helpers
# ---------------------------------------------------------------------------

def get_all_price_items_as_text() -> str:
    items = get_active_price_items()
    if not items:
        return "Прайс-лист пуст."
    lines = ["Наименование | Ед. | Цена BYN | Категория"]
    for i in items:
        lines.append(f"{i['name']} | {i['unit']} | {i['my_price']} | {i['category']}")
    return "\n".join(lines)


def get_search_settings_as_text() -> str:
    s = get_search_settings()
    return (
        f"Минимальный бюджет: {s['min_budget']} BYN\n"
        f"Минимальный бюджет (закупки из одного источника): {s['min_budget_single_source']} BYN\n"
        f"Порог релевантности X: {s['x_threshold']}%\n"
        f"Минимальная маржа Y: {s['y_threshold']}%\n"
        f"Регионы: {', '.join(s['regions'])}"
    )


def get_suitable_tenders_summary() -> str:
    tenders = get_last_suitable_tenders(limit=10)
    if not tenders:
        return "Подходящих тендеров пока нет."
    lines = []
    for t in tenders:
        lines.append(
            f"- {t['title'][:80]} | {t['budget_byn']} BYN | "
            f"K={t['k_score']:.0%} L={t['l_score']:.0%} M={t['m_score']:.0%} S={t['s_score']:.2f}"
        )
    return "\n".join(lines)
