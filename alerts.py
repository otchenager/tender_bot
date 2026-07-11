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

# Challenge-service markers appear only on anti-bot interstitials
# (Cloudflare / DDoS-Guard) — safe to trust at any page size.
_CHALLENGE_MARKERS = ("checking your browser", "ddos-guard", "cf-challenge")

# Widget/text markers also appear legitimately inside login forms and even
# site chrome (goszakupki's menu has a "Проверка браузера" link on EVERY
# page — verified live 2026-07-11), so they only count when the captcha
# essentially IS the page: challenge screens are tiny, real listing/detail
# pages are tens of KB. This distinguishes "site served a CAPTCHA" from
# both "page empty" and "normal page that merely mentions captcha".
_WIDGET_MARKERS = (
    "g-recaptcha", "hcaptcha", "smartcaptcha", "captcha",
    "подтвердите, что вы не робот",
)


def looks_like_captcha(html: str | None) -> bool:
    if not html:
        return False
    low = html.lower()
    if any(m in low for m in _CHALLENGE_MARKERS):
        return True
    return len(html) < 5000 and any(m in low for m in _WIDGET_MARKERS)


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
