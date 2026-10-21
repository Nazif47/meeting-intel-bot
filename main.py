"""
Meeting Intelligence Bot — Cloud Run Job entry point.

Execution order:
  1. Bootstrap all Google API service clients.
  2. Read the 'Config' Sheet tab to discover which users to process.
  3. Fetch already-processed IDs from the Database tab (deduplication).
  4. Workflow A — Google Meet: pull audit logs → fetch Drive transcripts → AI.
  5. Workflow B — 3CX Audio:  list GCS bucket → pass URI to AI (no download).
  6. Append every processed record to the 'Database' Sheet tab.
  7. Email department reports to managers; email master report to admin.

The job exits with code 0 on success, 1 on a fatal bootstrap error.
Individual meeting failures are logged and skipped so one bad record
never aborts the whole run.
"""
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config  # noqa: F401 — triggers .env load before any other import

from services.auth import (
    build_admin_service,
    build_drive_service,
    build_gmail_service,
    build_gcs_client,
    build_sheets_service,
)
from services import drive as drive_svc
from services import gcs as gcs_svc
from services import ai as ai_svc
from services import sheets as sheets_svc
from services import email as email_svc
from services import pdf as pdf_svc
from services import tcx_api as tcx_svc

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Privacy constants ──────────────────────────────────────────────────────────
_PRIVATE_BUCKET = "Private_Executive"

# Departments that belong to the executive but get standalone PDFs + individual emails
# (not merged into "Management" and not included in the master report).
_EXEC_DIRECT_DEPTS: frozenset[str] = frozenset({"Renewals", "Development"})


# ── Orchestration ──────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("══════════ Meeting Intelligence Bot — Job Start ══════════")

    # ── Bootstrap ──────────────────────────────────────────────────────────────
    try:
        sheets_service = build_sheets_service()
        gmail_service = build_gmail_service()
        admin_service = build_admin_service()
        gcs_client = build_gcs_client()
    except Exception as exc:
        logger.critical("Service bootstrap failed — aborting: %s", exc)
        sys.exit(1)

    # ── Config ─────────────────────────────────────────────────────────────────
    users = sheets_svc.read_config(sheets_service)
    if not users:
        logger.warning("No users marked 'Yes' in Config tab. Nothing to process.")
        return

    # user_map: lowercase email → user_info  (used by Workflow A — Meet)
    user_map: dict[str, dict] = {u["email"]: u for u in users}
    target_emails: set[str] = set(user_map.keys())

    # ext_map: 3CX extension string → user_info  (used by Workflow B — 3CX)
    ext_map: dict[str, dict] = {
        u["extension"]: u for u in users if u.get("extension")
    }
    logger.info("Config: %d extensions whitelisted for 3CX processing.", len(ext_map))

    # ── Deduplication ──────────────────────────────────────────────────────────
    processed_ids: set[str] = sheets_svc.fetch_processed_ids(sheets_service)
    logger.info("Deduplication: %d records already in Database.", len(processed_ids))

    # ── Executive privacy identifiers ────────────────────────────────────────────────
    # Calls where from_ or to matches any of these are routed exclusively to
    # the Private_Executive bucket, never surfaced to mid-level managers.
    exec_ids: frozenset[str] = frozenset(
        i.strip()
        for i in os.getenv("EXECUTIVE_IDENTIFIERS", "").split(",")
        if i.strip()
    )
    logger.info("Privacy: %d executive identifiers loaded.", len(exec_ids))

    all_records: list[dict] = []

    # ══════════════════════════════════════════════════════════════════════════
    # Workflow A — Google Meet
    # ══════════════════════════════════════════════════════════════════════════
    logger.info("── Workflow A: Google Meet ──")
    meet_events = drive_svc.get_meet_events(admin_service, target_emails)

    for activity in meet_events:
        try:
            _process_meet_event(
                activity=activity,
                user_map=user_map,
                sheets_service=sheets_service,
                all_records=all_records,
                processed_ids=processed_ids,
                exec_ids=exec_ids,
            )
        except Exception as exc:
            logger.error("Unhandled error processing Meet activity: %s", exc, exc_info=True)

    # ══════════════════════════════════════════════════════════════════════════
    # Workflow B — 3CX Audio (GCS)
    # ══════════════════════════════════════════════════════════════════════════
    logger.info("── Workflow B: 3CX Audio (GCS) ──")
    audio_files = gcs_svc.get_recent_audio_files(gcs_client)

    for audio in audio_files:
        try:
            _process_audio_file(
                audio=audio,
                ext_map=ext_map,
                sheets_service=sheets_service,
                all_records=all_records,
                processed_ids=processed_ids,
                exec_ids=exec_ids,
            )
        except Exception as exc:
            logger.error("Unhandled error processing audio %s: %s", audio.get("file_name"), exc, exc_info=True)

    # ══════════════════════════════════════════════════════════════════════════
    # Group records by department (flat list per dept).
    #
    # Priority order for bucket override:
    #   1. _private_exec flag  → "Private_Executive"  (isolated, never in master)
    #   2. manager is executive → "Management"      (included in master)
    #   3. otherwise            → listed department
    #
    # Within each department the PDF layer is responsible for the full
    # hierarchy: Date → Agent → [records].  Records carry a "date" field
    # extracted from the filename / event timestamp so pdf.py can sort and
    # group without any additional mapping here.
    # ══════════════════════════════════════════════════════════════════════════
    by_dept: dict[str, list[dict]] = defaultdict(list)
    for rec in all_records:
        if rec.get("_private_exec"):
            dept_key = _PRIVATE_BUCKET
        elif (
            rec.get("manager_email", "").lower() == config.ADMIN_EMAIL.lower()
            and rec["department"] not in _EXEC_DIRECT_DEPTS
        ):
            dept_key = "Management"
        else:
            dept_key = rec["department"]
        by_dept[dept_key].append(rec)

    # ══════════════════════════════════════════════════════════════════════════
    # Contact Enrichment — 3CX V20 API lookup (no-op if not configured)
    # Sweeps all records and fills contact_name / company_name where possible.
    # ══════════════════════════════════════════════════════════════════════════
    logger.info("── Contact Enrichment (3CX API) ──")
    for rec in all_records:
        if rec.get("contact_name") is None:
            contact = tcx_svc.lookup_contact(rec.get("to", ""))
            rec["contact_name"] = contact["name"]
            rec["company_name"] = contact["company"]

    # ══════════════════════════════════════════════════════════════════════════
    # PDF Generation — one PDF per department
    # ══════════════════════════════════════════════════════════════════════════
    logger.info("── PDF Generation ──")
    pdf_paths: dict[str, str] = {}
    try:
        pdf_paths = pdf_svc.generate_department_pdfs(dict(by_dept), _today())
        logger.info("Generated %d department PDF(s).", len(pdf_paths))
    except Exception as exc:
        logger.error("PDF generation failed — emails will send without attachments: %s", exc)

    # ══════════════════════════════════════════════════════════════════════════
    # Report Dispatch
    # ══════════════════════════════════════════════════════════════════════════
    logger.info("── Email Dispatch ──")
    _dispatch_reports(gmail_service, by_dept, pdf_paths)

    # Delete all temp PDFs now that all emails have been sent.
    for path in pdf_paths.values():
        try:
            os.unlink(path)
        except OSError as exc:
            logger.warning("Could not delete temp PDF %s: %s", path, exc)

    logger.info(
        "══════════ Job Complete — %d records processed ══════════",
        len(all_records),
    )


# ── Workflow helpers ───────────────────────────────────────────────────────────

def _process_meet_event(
    activity: dict,
    user_map: dict[str, dict],
    sheets_service,
    all_records: list[dict],
    processed_ids: set[str],
    exec_ids: frozenset[str],
) -> None:
    meta = drive_svc.parse_meet_event(activity)
    host = meta["host_email"]
    transcript_link = f"https://meet.google.com/{meta['meeting_code']}"

    if transcript_link in processed_ids:
        logger.info("Skipping (already processed): Meet %s", meta["meeting_code"])
        return

    user_info = user_map.get(host, {})

    drive_service = build_drive_service(subject=host)
    transcript_text = drive_svc.get_transcript_text(
        drive_service, meta["meeting_code"], host
    )

    if not transcript_text:
        logger.warning("No transcript for meeting %s — skipped.", meta["meeting_code"])
        return

    ai_result = ai_svc.process_transcript(transcript_text)
    record = _build_record(
        date=meta["start_time"][:10] if meta["start_time"] else _today(),
        time=_parse_time_iso(meta.get("start_time", "")),
        department=user_info.get("department", "Unknown"),
        from_=host,
        to=meta["participants"],
        duration=meta["duration_str"],
        ai_result=ai_result,
        transcript_link=transcript_link,
        manager_email=user_info.get("manager_email", ""),
        private_exec=_is_exec_call([host, meta["participants"]], exec_ids),
        source="meet",
    )

    sheets_svc.append_to_database(sheets_service, record)
    all_records.append(record)
    logger.info(
        "Meet processed: %s | sentiment=%s | escalated=%s | private=%s",
        meta["meeting_code"],
        record["sentiment"],
        record["_escalated"],
        record["_private_exec"],
    )


def _process_audio_file(
    audio: dict,
    ext_map: dict[str, dict],
    sheets_service,
    all_records: list[dict],
    processed_ids: set[str],
    exec_ids: frozenset[str],
) -> None:
    from_ext = audio.get("from", "").strip()

    user_info = ext_map.get(from_ext)
    if user_info is None:
        logger.info(
            "3CX skipped (extension not whitelisted): %s", audio.get("file_name")
        )
        return

    gcs_uri = audio["gcs_uri"]
    if gcs_uri in processed_ids:
        logger.info("Skipping (already processed): %s", audio.get("file_name"))
        return

    caller_name = audio.get("caller_name", "")
    from_label = (
        f"{caller_name} (ext. {from_ext})"
        if caller_name and caller_name != "Unknown"
        else from_ext
    )

    ai_result = ai_svc.process_audio(gcs_uri, mime_type=audio["mime_type"])
    record = _build_record(
        date=audio.get("date") or _today(),
        time=audio.get("time", ""),
        department=user_info["department"],
        from_=from_label,
        to=audio.get("to", "Unknown"),
        duration=audio.get("duration", "N/A"),
        ai_result=ai_result,
        transcript_link=gcs_uri,
        manager_email=user_info["manager_email"],
        private_exec=_is_exec_call([from_ext, audio.get("to", "")], exec_ids),
    )

    sheets_svc.append_to_database(sheets_service, record)
    all_records.append(record)
    logger.info(
        "3CX processed: %s → %s | sentiment=%s | escalated=%s | private=%s",
        from_label,
        audio.get("to"),
        record["sentiment"],
        record["_escalated"],
        record["_private_exec"],
    )


def _dispatch_reports(
    gmail_service,
    by_dept: dict[str, list[dict]],
    pdf_paths: dict[str, str],
) -> None:
    """
    Routing hierarchy:
      Private_Executive  → isolated email to executive only; excluded from master.
      Mid-level mgrs → one email with their department's PDF only.
      Executive      → ONE master email with all non-private PDFs attached.
    """
    # ── Private executive report — sent in isolation, never in master ──────────
    private_pdf = pdf_paths.get(_PRIVATE_BUCKET)
    if private_pdf:
        email_svc.send_department_report(
            gmail_service,
            manager_email=config.ADMIN_EMAIL,
            department=_PRIVATE_BUCKET,
            pdf_path=private_pdf,
        )

    # Build the master-eligible set (excludes Private_Executive and direct-to-executive depts)
    master_pdf_paths = {
        dept: path
        for dept, path in pdf_paths.items()
        if dept != _PRIVATE_BUCKET and dept not in _EXEC_DIRECT_DEPTS
    }

    # ── Renewals / Development — standalone emails direct to executive────────
    for dept in _EXEC_DIRECT_DEPTS:
        dept_pdf = pdf_paths.get(dept)
        if dept_pdf:
            email_svc.send_department_report(
                gmail_service,
                manager_email=config.ADMIN_EMAIL,
                department=dept,
                pdf_path=dept_pdf,
            )

    # ── Per-manager emails ─────────────────────────────────────────────────
    for dept, records in by_dept.items():
        if dept == _PRIVATE_BUCKET or dept in _EXEC_DIRECT_DEPTS:
            continue
        manager_email = records[0].get("manager_email", "").strip()
        # Management bucket is covered by the master report below.
        if manager_email.lower() == config.ADMIN_EMAIL.lower():
            continue
        email_svc.send_department_report(
            gmail_service,
            manager_email,
            dept,
            pdf_path=master_pdf_paths.get(dept),
        )

    # ── Master report to executive (all non-private, non-direct PDFs) ──────────
    email_svc.send_master_report(gmail_service, dept_pdf_paths=master_pdf_paths)


# ── Utility helpers ────────────────────────────────────────────────────────────

def _build_record(
    date: str,
    time: str,
    department: str,
    from_: str,
    to: str,
    duration: str,
    ai_result: dict,
    transcript_link: str,
    manager_email: str,
    private_exec: bool = False,
    contact_name: str | None = None,
    company_name: str | None = None,
    source: str = "3cx",
) -> dict:
    return {
        "date": date,
        "time": time,
        "department": department,
        "from": from_,
        "to": to,
        "contact_name": contact_name,
        "company_name": company_name,
        "duration": duration,
        "sentiment": ai_result.get("Sentiment", "Neutral"),
        "summary": ai_result.get("Summary", ""),
        "action_points": ai_result.get("Action Points", []),
        "full_transcript": ai_result.get("full_transcript") or ai_result.get("Full_Transcript", ""),
        "is_actual_conversation": ai_result.get("is_actual_conversation", True),
        "transcript_link": transcript_link,
        "manager_email": manager_email,
        "_escalated": ai_result.get("_escalated", False),
        "_private_exec": private_exec,
        "source": source,
    }


def _is_exec_call(values: list[str], exec_ids: frozenset[str]) -> bool:
    """Return True if any value in *values* matches a known executive identifier."""
    return any(v.strip() in exec_ids for v in values if v)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_time_iso(iso_timestamp: str) -> str:
    """Parse a UTC ISO 8601 timestamp, apply TIMEZONE_OFFSET, return HH:MM AM/PM."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        dt = dt + timedelta(hours=config.TIMEZONE_OFFSET)
        return dt.strftime("%I:%M %p")
    except (ValueError, TypeError):
        return ""


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
