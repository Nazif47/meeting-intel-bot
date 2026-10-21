"""
Authentication helpers.

Two auth patterns are used:
  1. Domain-Wide Delegation (DWD) — service account impersonates a Workspace
     user to access Admin SDK, Drive, Sheets, and Gmail.
  2. Direct service account — used for GCS (no impersonation needed).

The single service account JSON key file drives both patterns.
"""
import logging
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import storage

import config

logger = logging.getLogger(__name__)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _dwd_credentials(
    scopes: list[str],
    subject: str,
) -> service_account.Credentials:
    """Return DWD credentials impersonating *subject* with the given scopes."""
    creds = service_account.Credentials.from_service_account_file(
        config.SERVICE_ACCOUNT_KEY_FILE,
        scopes=scopes,
    )
    return creds.with_subject(subject)


# ── Public builders ────────────────────────────────────────────────────────────

def build_admin_service():
    """
    Admin SDK Reports v1 — impersonates the super-admin.
    Used to read Google Meet audit logs for all domain users.
    """
    creds = _dwd_credentials(
        scopes=[config.SCOPE_ADMIN_REPORTS],
        subject=config.ADMIN_EMAIL,
    )
    return build("admin", "reports_v1", credentials=creds, cache_discovery=False)


def build_drive_service(subject: str):
    """
    Drive API v3 — impersonates a specific user so we can read their
    Meet transcript files without altering any permissions.
    """
    creds = _dwd_credentials(
        scopes=[config.SCOPE_DRIVE],
        subject=subject,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def build_sheets_service():
    """
    Sheets API v4 — impersonates the admin who owns the tracker sheet.
    Grants read (Config tab) and append (Database tab) access.
    """
    creds = _dwd_credentials(
        scopes=[config.SCOPE_SHEETS],
        subject=config.ADMIN_EMAIL,
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def build_gmail_service():
    """
    Gmail API v1 — impersonates EMAIL_SENDER so outbound messages come
    from a real Workspace address (not the service account).
    """
    sender = os.getenv("EMAIL_SENDER") or config.EMAIL_SENDER
    base_creds = service_account.Credentials.from_service_account_file(
        config.SERVICE_ACCOUNT_KEY_FILE,
        scopes=[config.SCOPE_GMAIL],
    )
    creds = base_creds.with_subject(sender)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_gcs_client() -> storage.Client:
    """
    GCS client — uses the service account directly (no impersonation).
    Cloud Run jobs inherit ADC from the attached SA; locally we use the key file.
    """
    return storage.Client.from_service_account_json(config.SERVICE_ACCOUNT_KEY_FILE)
