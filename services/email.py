"""
Email dispatch via Gmail API.

Routing rules (CLAUDE.md — strict):
  - Department reports  →  individual manager only (one dept PDF attached).
  - Master report       →  MASTER_REPORT_RECIPIENT (Executive) only, all dept
                           PDFs attached in one email.
  - Admin is NEVER CC'd on department-level emails.

Email bodies are brief and contain no raw data or tables.
All report content lives in the attached PDFs.
"""
import base64
import logging
import os
from datetime import datetime, timezone
# Fully-qualified imports to avoid shadowing the stdlib 'email' package
import email.mime.application
import email.mime.multipart
import email.mime.text

from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────────────

def send_department_report(
    gmail_service,
    manager_email: str,
    department: str,
    pdf_path: str | None = None,
) -> None:
    """
    Send a brief notification email to the department manager.
    Attaches the department PDF when available.
    """
    if not manager_email:
        logger.warning("No manager email for '%s' — skipping department report.", department)
        return

    date    = _today()
    subject = _dept_subject(department, date)
    html    = _dept_html(department, date)

    attachments = []
    if pdf_path:
        attachments.append((pdf_path, f"{_filename_safe(department)} - {date} - Call Intelligence Report.pdf"))

    _send_email(gmail_service, to=manager_email, subject=subject,
                html=html, attachments=attachments)


def send_master_report(
    gmail_service,
    dept_pdf_paths: dict[str, str] | None = None,
    recipients: list[str] | None = None,
) -> None:
    """
    Send a brief master notification with ALL department PDFs attached.
    recipients defaults to config.MASTER_REPORT_RECIPIENTS (supports multiple).
    Managers are never included here — they receive individual department emails.
    """
    date       = _today()
    dept_paths = dept_pdf_paths or {}
    to_list    = recipients or config.MASTER_REPORT_RECIPIENTS
    subject    = f"Manager's Call Report - {date}"
    html       = _master_html(list(dept_paths.keys()), date)

    attachments = [
        (path, f"{_filename_safe(dept)} - {date} - Call Intelligence Report.pdf")
        for dept, path in dept_paths.items()
    ]

    _send_email(
        gmail_service,
        to=", ".join(to_list),
        subject=subject,
        html=html,
        attachments=attachments,
    )


# ── Internal send ──────────────────────────────────────────────────────────────

def _send_email(
    gmail_service,
    to: str,
    subject: str,
    html: str,
    attachments: list[tuple[str, str]] | None = None,
) -> None:
    """
    Build and send a MIME email.

    attachments: list of (file_path, display_filename) tuples.

    MIME structure:
      With attachments:  multipart/mixed
                           ├─ multipart/alternative → text/html
                           └─ application/pdf (one per attachment)
      Without:           multipart/alternative → text/html
    """
    if attachments:
        outer = email.mime.multipart.MIMEMultipart("mixed")
        alt   = email.mime.multipart.MIMEMultipart("alternative")
        alt.attach(email.mime.text.MIMEText(html, "html", "utf-8"))
        outer.attach(alt)
        for file_path, filename in attachments:
            try:
                with open(file_path, "rb") as fh:
                    part = email.mime.application.MIMEApplication(
                        fh.read(), _subtype="pdf"
                    )
                part.add_header("Content-Disposition", "attachment",
                                filename=filename)
                outer.attach(part)
            except OSError as exc:
                logger.error("Could not read PDF for attachment (%s): %s",
                             file_path, exc)
    else:
        outer = email.mime.multipart.MIMEMultipart("alternative")
        outer.attach(email.mime.text.MIMEText(html, "html", "utf-8"))

    outer["Subject"] = subject
    outer["From"]    = config.EMAIL_SENDER
    outer["To"]      = to

    raw = base64.urlsafe_b64encode(outer.as_bytes()).decode("utf-8")
    try:
        gmail_service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        n = len(attachments) if attachments else 0
        logger.info("Email sent -> %s | %s | %d attachment(s)", to, subject, n)
    except HttpError as exc:
        logger.error("Gmail send failed (to=%s): %s", to, exc)


# ── HTML body templates ────────────────────────────────────────────────────────

def _dept_html(department: str, date: str) -> str:
    dept_esc = _esc(department)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; font-size: 14px; color: #333; margin: 32px; }}
  h2   {{ color: #2c3e50; }}
  ul   {{ line-height: 1.8; }}
  p    {{ line-height: 1.6; }}
  .footer {{ color: #aaa; font-size: 11px; margin-top: 24px; }}
</style>
</head>
<body>
  <h2>Daily Call Intelligence Report</h2>
  <p>Please find attached the <strong>{dept_esc}</strong> department report for <strong>{_esc(date)}</strong>.</p>
  <p>The attached PDF contains:</p>
  <ul>
    <li>Agent activity summary (calls attempted vs. actual conversations)</li>
    <li>AI-generated analysis for each processed call</li>
    <li>Full transcripts for completed conversations</li>
  </ul>
  <p>Please review and follow up on any required action items.</p>
  <p class="footer">Generated by Meeting Intelligence Bot</p>
</body>
</html>"""


def _master_html(departments: list[str], date: str) -> str:
    dept_items = "".join(f"<li>{_esc(d)}</li>" for d in departments)
    n = len(departments)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; font-size: 14px; color: #333; margin: 32px; }}
  h2   {{ color: #2c3e50; }}
  ul   {{ line-height: 1.8; }}
  p    {{ line-height: 1.6; }}
  .footer {{ color: #aaa; font-size: 11px; margin-top: 24px; }}
</style>
</head>
<body>
  <h2>Master Daily Call Intelligence Report</h2>
  <p>Please find attached the daily reports for all <strong>{n}</strong> department(s), covering <strong>{_esc(date)}</strong>.</p>
  <p>Departments covered:</p>
  <ul>{dept_items}</ul>
  <p>Each PDF contains an agent activity summary, AI-generated call analysis, and full transcripts.</p>
  <p class="footer">Generated by Meeting Intelligence Bot</p>
</body>
</html>"""


# ── Utilities ──────────────────────────────────────────────────────────────────

def _dept_subject(department: str, date: str) -> str:
    """Return the executive-style subject line for a department email."""
    if department == "Private_Executive":
        return f"Executive Call Report - {date}"
    return f"{department} Team Call Report - {date}"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _safe_name(name: str) -> str:
    """Convert a department name to a safe filename fragment (legacy, underscored)."""
    return name.replace(" ", "_").replace("/", "-").replace("\\", "-")


def _filename_safe(name: str) -> str:
    """Make a display name safe for a filename while keeping spaces readable."""
    return name.replace("/", "-").replace("\\", "-").replace(":", "-")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
