"""Email notification — one email per suitable tender."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import db
from config import EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD
from logger import get_logger

log = get_logger("notifier")

VPS_URL = "http://YOUR_VPS_IP:5000"


def _fmt(value, suffix="") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.0f}".replace(",", " ") + suffix
    except (TypeError, ValueError):
        return str(value)


def _format_top_positions(positions: list[dict]) -> str:
    if not positions:
        return "—"
    lines = []
    for i, p in enumerate(positions, 1):
        lines.append(
            f"  {i}. {p.get('smeta_name', '?')} "
            f"({p.get('smeta_unit', '')}) × {p.get('smeta_quantity', '')} "
            f"= {_fmt(p.get('smeta_total_cost'))} BYN"
        )
    return "\n".join(lines)


def send_email(tender_id: int) -> bool:
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        log.warning("Email credentials not configured, skipping notification")
        return False

    tender = db.get_tender(tender_id)
    if not tender:
        return False

    top5 = db.get_top_positions(tender_id, limit=5)

    s_score = tender.get("s_score")
    score_str = f"{s_score * 100:.0f}" if s_score is not None else "—"
    title_short = (tender.get("title") or "")[:50]

    subject = f"🏗 Новый тендер: {title_short} | Балл: {score_str}"

    body = f"""\
Найден подходящий тендер!

📋 {tender.get('title')}
💰 Бюджет: {_fmt(tender.get('budget_byn'))} BYN
📍 Регион: {tender.get('region') or '—'}
📅 Дедлайн: {tender.get('deadline') or '—'}

📊 Анализ:
   Совпадение K:    {(tender.get('k_score') or 0):.1%}
   Релевантность L: {(tender.get('l_score') or 0):.1%}
   Маржа M:         {(tender.get('m_score') or 0):.1%}
   Балл S:          {score_str} / 100

💬 AI комментарий:
{tender.get('ai_comment') or '—'}

🔝 Топ-5 позиций:
{_format_top_positions(top5)}

👉 Дашборд: {VPS_URL}/tender/{tender_id}
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f"Email sent for tender {tender_id} to {EMAIL_TO}")
        return True
    except Exception as e:
        log.error(f"Email send error: {e}")
        return False
