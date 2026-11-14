"""Settings shared by every module."""
import os
from pathlib import Path

import truststore
from dotenv import load_dotenv

# Use the operating system's certificate store so HTTPS works behind company proxies
truststore.inject_into_ssl()
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DB_URL = os.getenv("DB_URL", f"sqlite:///{ROOT / 'leave_tracker.db'}")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
# Used when the main model is rate-limited; on Groq each model has its own per-minute token quota
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "openai/gpt-oss-20b")

# Yearly allowance per leave type (working days)
ALLOWANCE = {"casual": 12, "sick": 6, "earned": 10}
LEAVE_TYPES = tuple(ALLOWANCE)

# How far ahead the "nearest free dates" search looks
SUGGESTION_WINDOW_DAYS = 60
