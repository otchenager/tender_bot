"""Слой работы с SQLite базой данных."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "tenders.db"

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """Возвращает соединение с БД для текущего потока (с WAL режимом)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db():
    """Создаёт таблицы базы данных, если их ещё нет."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tenders (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            description TEXT,
            amount REAL,
            deadline TEXT,
            posted_at TEXT,
            customer TEXT,
            customer_address TEXT,
            url TEXT,
            category TEXT,
            matched_group TEXT,
            priority INTEGER,
            okrb_code TEXT,
            financing TEXT,
            payment_terms TEXT,
            tender_type TEXT,
            raw_data TEXT,
            parsed_at TEXT,
            sent INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_analysis (
            tender_id TEXT PRIMARY KEY,
            analysis TEXT,
            score INTEGER,
            verdict TEXT,
            margin_byn REAL,
            margin_pct REAL,
            analyzed_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            unit TEXT,
            my_price REAL,
            category TEXT,
            occurrences INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tender_id TEXT,
            reaction TEXT,
            reason TEXT,
            comment TEXT,
            created_at TEXT
        )
    """)

    conn.commit()


def is_seen(tender_id: str) -> bool:
    """Проверяет, был ли тендер уже сохранён в базе."""
    conn = get_conn()
    cur = conn.execute("SELECT 1 FROM tenders WHERE id = ?", (tender_id,))
    return cur.fetchone() is not None


def save_tender(tender: dict) -> bool:
    """
    Сохраняет тендер в базу. Возвращает True, если тендер новый
    и был сохранён, False - если уже существовал.
    """
    if is_seen(tender["id"]):
        return False

    conn = get_conn()
    raw_data = tender.get("raw_data")
    if not isinstance(raw_data, str):
        raw_data = json.dumps(raw_data or {}, ensure_ascii=False)

    conn.execute("""
        INSERT INTO tenders (
            id, source, title, description, amount, deadline, posted_at,
            customer, customer_address, url, category, matched_group,
            priority, okrb_code, financing, payment_terms, tender_type,
            raw_data, parsed_at, sent
        ) VALUES (
            :id, :source, :title, :description, :amount, :deadline, :posted_at,
            :customer, :customer_address, :url, :category, :matched_group,
            :priority, :okrb_code, :financing, :payment_terms, :tender_type,
            :raw_data, :parsed_at, 0
        )
    """, {
        "id": tender["id"],
        "source": tender.get("source"),
        "title": tender.get("title"),
        "description": tender.get("description"),
        "amount": tender.get("amount"),
        "deadline": tender.get("deadline"),
        "posted_at": tender.get("posted_at"),
        "customer": tender.get("customer"),
        "customer_address": tender.get("customer_address"),
        "url": tender.get("url"),
        "category": tender.get("category"),
        "matched_group": tender.get("matched_group"),
        "priority": tender.get("priority"),
        "okrb_code": tender.get("okrb_code"),
        "financing": tender.get("financing"),
        "payment_terms": tender.get("payment_terms"),
        "tender_type": tender.get("tender_type"),
        "raw_data": raw_data,
        "parsed_at": _now(),
    })
    conn.commit()
    return True


def save_analysis(tender_id: str, analysis: str, score: int, verdict: str,
                   margin_byn=None, margin_pct=None):
    """Сохраняет результат AI-анализа тендера."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO ai_analysis (
            tender_id, analysis, score, verdict, margin_byn, margin_pct, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tender_id) DO UPDATE SET
            analysis=excluded.analysis,
            score=excluded.score,
            verdict=excluded.verdict,
            margin_byn=excluded.margin_byn,
            margin_pct=excluded.margin_pct,
            analyzed_at=excluded.analyzed_at
    """, (tender_id, analysis, score, verdict, margin_byn, margin_pct, _now()))
    conn.commit()


def mark_sent(tender_id: str):
    """Помечает тендер как отправленный в Telegram."""
    conn = get_conn()
    conn.execute("UPDATE tenders SET sent = 1 WHERE id = ?", (tender_id,))
    conn.commit()


def save_feedback(tender_id: str, reaction: str, reason: str = None, comment: str = None):
    """Сохраняет обратную связь пользователя по тендеру."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO feedback (tender_id, reaction, reason, comment, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (tender_id, reaction, reason, comment, _now()))
    conn.commit()


def get_feedback_stats() -> list:
    """Возвращает агрегированную статистику по обратной связи."""
    conn = get_conn()
    cur = conn.execute("""
        SELECT reaction, reason, COUNT(*) as count
        FROM feedback
        GROUP BY reaction, reason
        ORDER BY count DESC
    """)
    return [dict(row) for row in cur.fetchall()]


def get_unsent_analyzed(limit: int = 3) -> list:
    """Возвращает топ-N проанализированных, но ещё не отправленных тендеров."""
    conn = get_conn()
    cur = conn.execute("""
        SELECT t.*, a.analysis, a.score, a.verdict, a.margin_byn, a.margin_pct
        FROM tenders t
        JOIN ai_analysis a ON a.tender_id = t.id
        WHERE t.sent = 0
        ORDER BY a.score DESC
        LIMIT ?
    """, (limit,))
    return [dict(row) for row in cur.fetchall()]


def get_price_items() -> list:
    """Возвращает список всех ценовых позиций заказчика."""
    conn = get_conn()
    cur = conn.execute("SELECT * FROM price_items ORDER BY category, name")
    return [dict(row) for row in cur.fetchall()]


def save_price_item(name: str, unit: str, my_price: float, category: str):
    """Создаёт новую ценовую позицию или обновляет цену существующей."""
    conn = get_conn()
    cur = conn.execute(
        "SELECT id FROM price_items WHERE name = ? AND unit = ?", (name, unit)
    )
    row = cur.fetchone()
    if row:
        conn.execute("""
            UPDATE price_items SET my_price = ?, category = ?, updated_at = ?
            WHERE id = ?
        """, (my_price, category, _now(), row["id"]))
    else:
        conn.execute("""
            INSERT INTO price_items (name, unit, my_price, category, occurrences, updated_at)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (name, unit, my_price, category, _now()))
    conn.commit()


def update_price_item_occurrence(name: str):
    """Увеличивает счётчик встречаемости позиции в сметах на 1."""
    conn = get_conn()
    cur = conn.execute("SELECT id FROM price_items WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        conn.execute("""
            UPDATE price_items SET occurrences = occurrences + 1, updated_at = ?
            WHERE id = ?
        """, (_now(), row["id"]))
    else:
        conn.execute("""
            INSERT INTO price_items (name, unit, my_price, category, occurrences, updated_at)
            VALUES (?, '', NULL, '', 1, ?)
        """, (name, _now()))
    conn.commit()
