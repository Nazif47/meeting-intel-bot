"""
Central configuration loaded from environment variables.
All secrets and environment-specific values live in .env — never hardcoded here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Google Cloud ───────────────────────────────────────────────────────────────
PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
LOCATION: str = os.environ.get("GCP_LOCATION", "us-central1")
GCS_BUCKET_NAME: str = os.environ["GCS_BUCKET_NAME"]

# Path to the service account JSON key used for both DWD and GCS auth.
# In Cloud Run Jobs this is mounted as a secret volume.
SERVICE_ACCOUNT_KEY_FILE: str = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS", "service_account.json"
)

# ── Google Workspace ───────────────────────────────────────────────────────────
# Super-admin email the service account impersonates via Domain-Wide Delegation.
ADMIN_EMAIL: str = os.environ["ADMIN_EMAIL"]

# ── Google Sheets ──────────────────────────────────────────────────────────────
SPREADSHEET_ID: str = os.environ["SPREADSHEET_ID"]
CONFIG_TAB: str = "Config"
DATABASE_TAB: str = "Database"

# ── Email ──────────────────────────────────────────────────────────────────────
EMAIL_SENDER: str = os.environ.get("EMAIL_SENDER", ADMIN_EMAIL)
MASTER_REPORT_RECIPIENT: str | None = os.environ.get("MASTER_REPORT_RECIPIENT")
# Optional additional recipient for the master report (e.g. a secondary admin).
# Leave blank or unset to send to MASTER_REPORT_RECIPIENT only.
_master_cc = os.environ.get("MASTER_REPORT_CC", "").strip()
MASTER_REPORT_RECIPIENTS: list[str] = [
    r for r in (MASTER_REPORT_RECIPIENT, _master_cc) if r
]

# ── AI Models (google-genai / Vertex AI backend) ───────────────────────────────
# DEFAULT_MODEL — fast, cost-efficient; used for 3CX audio analysis.
# COMPLEX_MODEL — higher capability; used directly for Google Meet transcripts
#                 and as the escalation target for negative-sentiment 3CX calls.
DEFAULT_MODEL: str = os.environ.get("PRIMARY_AI_MODEL", "gemini-2.5-flash")
COMPLEX_MODEL: str = os.environ.get("ESCALATION_AI_MODEL", "gemini-2.5-pro")

# Legacy aliases kept so any external references don't break during transition.
TRIAGE_MODEL    = DEFAULT_MODEL
DEEP_DIVE_MODEL = COMPLEX_MODEL

# Sentiments that trigger automatic escalation to the Complex model (3CX path)
ESCALATION_SENTIMENTS: frozenset[str] = frozenset({"Negative", "Angry"})

# ── Google API Scopes (for DWD credentials) ────────────────────────────────────
SCOPE_ADMIN_REPORTS: str = (
    "https://www.googleapis.com/auth/admin.reports.audit.readonly"
)
SCOPE_DRIVE: str = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_SHEETS: str = "https://www.googleapis.com/auth/spreadsheets"
SCOPE_GMAIL: str = "https://www.googleapis.com/auth/gmail.send"

# ── Job Parameters ─────────────────────────────────────────────────────────────
LOOKBACK_HOURS: int = int(os.environ.get("LOOKBACK_HOURS", "24"))

# Offset in hours to convert UTC timestamps to local time for PDF display.
# Supports fractional hours (e.g. 5.5 for IST).  Defaults to 0 (UTC).
TIMEZONE_OFFSET: float = float(os.environ.get("TIMEZONE_OFFSET", "0"))
