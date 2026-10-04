import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

COMPANY_DIR = ROOT / "company"
SANDBOX_DATA = ROOT / "sandbox" / "data"
MAILBOX_DIR = SANDBOX_DATA / "mailbox"
DRIVE_DIR = SANDBOX_DATA / "shared_drive"
OUTBOX_DIR = SANDBOX_DATA / "outbox"
RUNS_DIR = ROOT / "runs"
CACHE_DIR = ROOT / ".cache"
STATE_DB = CACHE_DIR / "operator_state.db"      # LangGraph checkpoints
PROCEDURE_DB = CACHE_DIR / "procedures.db"      # learned procedures

ERP_URL = os.getenv("ERP_URL", "http://127.0.0.1:8800").rstrip("/")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL = os.getenv("MODEL", "openai/gpt-4o-mini")
FALLBACK_MODELS = [m.strip() for m in os.getenv("FALLBACK_MODELS", "").split(",") if m.strip()]
LLM_CACHE = os.getenv("LLM_CACHE", "1") == "1"
HEADED = os.getenv("HEADED", "0") == "1"

MAX_AGENT_TURNS = int(os.getenv("MAX_AGENT_TURNS", "20"))
MAX_VERIFY_ATTEMPTS = 2

for d in (RUNS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)
