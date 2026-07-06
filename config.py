"""Project configuration — all runtime values come from .env."""

import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
EMAIL_FROM        = os.getenv("EMAIL_FROM", "")
EMAIL_TO          = os.getenv("EMAIL_TO", "")
EMAIL_PASSWORD    = os.getenv("EMAIL_PASSWORD", "")
DATABASE_URL      = os.getenv("DATABASE_URL", "postgresql://localhost/tender_bot")
INGEST_API_KEY    = os.getenv("INGEST_API_KEY", "")
RAILWAY_URL       = os.getenv("RAILWAY_URL", "")


def check_config() -> bool:
    ok = True
    if not ANTHROPIC_API_KEY:
        print("[config] ANTHROPIC_API_KEY not set in .env")
        ok = False
    if not DATABASE_URL:
        print("[config] DATABASE_URL not set in .env")
        ok = False
    if not INGEST_API_KEY:
        print("[config] INGEST_API_KEY not set in .env")
        ok = False
    return ok
