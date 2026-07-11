"""Operational email alerts from the VPS parser (CAPTCHA, blocks).

Standalone on purpose: unlike notifier.py this must not import db (the VPS
has no PostgreSQL). Credentials come straight from .env. Alerts are throttled
per key so a CAPTCHA that persists for hours produces one email, not one per
parse round.
"""

import os
import smtplib
import time
from email.mime.text import MIMEText

from dotenv import load_dotenv

from logger import get_logger

load_dotenv()

log = get_logger("alerts")

EMAIL_FROM     = os.getenv("EMAIL_FROM", "")
EMAIL_TO       = os.getenv("EMAIL_TO", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

_MIN_INTERVAL_S = 6 * 3600
_last_sent: dict[str, float] = {}


def send_alert(key: str, subject: str, body: str) -> bool:
    """Email an operational alert, at most once per _MIN_INTERVAL_S per key.
    Always logs, even when email is not configured — never fails silently."""
    now = time.time()
    if now - _last_sent.get(key, 0) < _MIN_INTERVAL_S:
        log.info(f"Alert '{key}' throttled (already sent recently): {subject}")
        return False
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        log.error(f"Alert '{key}' NOT emailed — EMAIL_* not configured: {subject} | {body}")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        _last_sent[key] = now
        log.info(f"Alert emailed: {subject}")
        return True
    except Exception as e:
        log.error(f"Alert email error for '{key}': {e}")
        return False


# ---------------------------------------------------------------------------
# CAPTCHA detection (used by both scrapers)
# ---------------------------------------------------------------------------

# Strong markers appear only on challenge pages; the generic "captcha" word
# alone is trusted only on tiny pages (challenge screens are small, real
# listing/detail pages are tens of KB and may mention captcha in a login
# widget) — this distinguishes "site served a CAPTCHA" from "page empty".
_STRONG_MARKERS = (
    "g-recaptcha", "hcaptcha", "smartcaptcha", "ddos-guard",
    "checking your browser", "подтвердите, что вы не робот",
    "проверка браузера", "cf-challenge",
)


def looks_like_captcha(html: str | None) -> bool:
    if not html:
        return False
    low = html.lower()
    if any(m in low for m in _STRONG_MARKERS):
        return True
    return "captcha" in low and len(html) < 5000


def alert_captcha(source: str, url: str):
    """Log + email 'bot hit a CAPTCHA, manual check needed'."""
    log.error(f"{source}: CAPTCHA page served for {url} — manual check needed")
    send_alert(
        f"{source}_captcha",
        f"Тендер-бот: CAPTCHA на {source}",
        f"Парсер получил CAPTCHA-страницу вместо данных.\n\n"
        f"Источник: {source}\nURL: {url}\n\n"
        f"Требуется ручная проверка: откройте сайт с белорусского IP и "
        f"пройдите проверку, либо дождитесь снятия ограничения. Парсер "
        f"продолжит попытки в следующих раундах.",
    )
