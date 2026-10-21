"""
Google Meet / Drive integration.

Workflow A — two steps:
  1. Query Admin SDK Reports for 'call_ended' events in the lookback window.
  2. Impersonate the meeting host on Drive to retrieve the auto-generated
     transcript document (read-only — we never mutate permissions).
"""
import logging
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)


# ── Audit Log Fetching ─────────────────────────────────────────────────────────

def get_meet_events(admin_service, target_emails: set[str]) -> list[dict]:
    """
    Query Admin SDK Reports for Meet 'call_ended' events across the whole
    domain, then filter to the users we are configured to process.

    Returns a list of raw activity dicts.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)
    ).isoformat()

    events: list[dict] = []
    page_token: str | None = None

    try:
        while True:
            resp = (
                admin_service.activities()
                .list(
                    userKey="all",
                    applicationName="meet",
                    eventName="call_ended",
                    startTime=since,
                    pageToken=page_token,
                )
                .execute()
            )

            for activity in resp.get("items", []):
                actor_email = (
                    activity.get("actor", {}).get("email", "").lower()
                )
                if actor_email in target_emails:
                    events.append(activity)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    except HttpError as exc:
        logger.error("Admin SDK error fetching Meet events: %s", exc)

    logger.info(
        "Admin SDK: found %d call_ended events for tracked users.", len(events)
    )
    return events


def parse_meet_event(activity: dict) -> dict:
    """
    Extract structured call metadata from a raw audit log activity item.

    Google Meet audit log parameters are returned as a flat list of
    {name, value} dicts inside each event entry.
    """
    params: dict[str, str] = {}
    for event in activity.get("events", []):
        for p in event.get("parameters", []):
            # Parameters can carry either a scalar 'value' or 'multiValue'
            params[p["name"]] = p.get("value") or ", ".join(
                p.get("multiValue", [])
            )

    duration_s = int(params.get("duration_seconds") or 0)

    return {
        "host_email": activity.get("actor", {}).get("email", "").lower(),
        "meeting_code": params.get("meeting_code", ""),
        "conference_id": params.get("conference_id", params.get("meeting_code", "")),
        "participants": params.get("display_name", params.get("identifier", "N/A")),
        "duration_seconds": duration_s,
        "duration_str": _fmt_duration(duration_s),
        # ISO timestamp lives in activity["id"]["time"]
        "start_time": activity.get("id", {}).get("time", ""),
    }


# ── Transcript Retrieval ───────────────────────────────────────────────────────

def get_transcript_text(drive_service, meeting_code: str, host_email: str) -> str | None:
    """
    Search the host's Drive for the auto-generated Meet transcript document
    and export it as plain text.

    Strategy (read-only — no permission changes):
      1. Narrow search: Google Doc whose name contains 'Transcript' AND
         whose body contains the meeting code.
      2. Broad fallback: any recent Google Doc named 'Transcript of …'.

    Returns the transcript string, or None if nothing is found.
    """
    try:
        transcript_id = (
            _search_transcript(drive_service, f"fullText contains '{meeting_code}'")
            or _search_transcript(drive_service, "name contains 'Transcript'")
        )

        if not transcript_id:
            logger.warning(
                "No transcript found for meeting %s (host: %s).",
                meeting_code,
                host_email,
            )
            return None

        raw = drive_service.files().export(
            fileId=transcript_id, mimeType="text/plain"
        ).execute()

        return raw.decode("utf-8") if isinstance(raw, bytes) else raw

    except HttpError as exc:
        logger.error(
            "Drive error retrieving transcript for %s: %s", meeting_code, exc
        )
        return None


def _search_transcript(drive_service, extra_clause: str) -> str | None:
    """Return the file ID of the first matching Meet transcript Doc, or None."""
    query = (
        f"mimeType='application/vnd.google-apps.document' "
        f"and name contains 'Transcript' "
        f"and {extra_clause} "
        f"and trashed=false"
    )
    resp = (
        drive_service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id, name, createdTime)",
            pageSize=1,
        )
        .execute()
    )
    files = resp.get("files", [])
    return files[0]["id"] if files else None


# ── Utilities ──────────────────────────────────────────────────────────────────

def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"
