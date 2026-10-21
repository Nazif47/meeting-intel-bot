"""
Google Cloud Storage integration for 3CX call recordings.

Workflow B rules (from CLAUDE.md):
  - Stream / pass GCS URIs directly to the AI model.
  - Do NOT download audio files into container memory.
  - Extract caller name, From extension, To extension/number, call date,
    and duration from the blob.

Actual bucket layout:
  Recordings/<extension>/<filename>
  e.g.  Recordings/203/[Employee Name]_203-212_20260514103000.wav
        Recordings/203/[Employee Name]_203-+15550000000_20260514114500.mp3

Filename format:  [{Name}]_{From}-{To}_{Timestamp}.{ext}
Timestamp format: YYYYMMDDHHmmss  (14 digits, no separators)

Duration is derived from blob.size using the known 3CX WAV encoding:
  8 kHz × 16-bit × 1 ch = 16,000 bytes/second — no download required.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from google.cloud import storage

import config

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# All recordings live under this GCS prefix
_GCS_PREFIX = "Recordings/"

_MIME_MAP: dict[str, str] = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
}

# Full metadata regex.
# To segment is now [^_]+ to accept +, %, letters, and other non-underscore
# characters that appear in international numbers or URL-encoded variants.
# e.g.  [Employee Name]_206-+15550000000_20260517085003(87).wav
#
# Groups: (name, from_ext, to_ext_or_number, timestamp_14, file_ext)
_FILENAME_RE = re.compile(
    r"^\[(.+?)\]"                     # [Name]      — agent/employee display name
    r"_(\d+)"                         # _From       — internal extension (e.g. 206)
    r"-([^_]+)"                       # -To         — any chars up to next underscore
    r"_(\d{14})"                      # _Timestamp  — YYYYMMDDHHmmss
    r"(?:\(\d+\))?"                   # optional (XX) 3CX dedup suffix — discarded
    r"\.(mp3|wav|ogg|m4a|flac)$",     # .ext
    re.IGNORECASE,
)

# Lightweight regex used only for the safety-net date check.
# Anchors to the end of the filename so it succeeds even when the name/number
# segments contain +, %, or other characters that trip up _FILENAME_RE.
_TIMESTAMP_RE = re.compile(
    r"_(\d{14})(?:\(\d+\))?\.(mp3|wav|ogg|m4a|flac)$",
    re.IGNORECASE,
)


# ── Public API ─────────────────────────────────────────────────────────────────

_SAFETY_NET_DAYS = 2   # today + yesterday + day before — never touch older files


def get_recent_audio_files(gcs_client: storage.Client) -> list[dict]:
    """
    List audio blobs under the 'Recordings/' prefix whose call date falls
    within the last _SAFETY_NET_DAYS days (today, yesterday, day before).

    The date is extracted from the 14-digit timestamp embedded in the filename
    using _TIMESTAMP_RE, which anchors to the end of the string and tolerates
    any characters in the name/number segments (including +, %, etc.).

    Deduplication (Database sheet) handles skipping already-processed files
    within the window.  Audio bytes are never downloaded.
    """
    today  = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=_SAFETY_NET_DAYS)

    bucket = gcs_client.bucket(config.GCS_BUCKET_NAME)
    results: list[dict] = []

    for blob in bucket.list_blobs(prefix=_GCS_PREFIX):
        # Skip folder placeholder objects (zero-byte, trailing slash)
        if blob.name.endswith("/"):
            continue
        if not _is_audio(blob.name):
            continue

        # ── 2-day safety net (checked before full metadata parse) ──────────
        # Use the lightweight _TIMESTAMP_RE so messy phone-number characters
        # in the name/To segments can never cause a fallback to "today".
        basename   = blob.name.rsplit("/", 1)[-1]
        ts_match   = _TIMESTAMP_RE.search(basename)
        if ts_match:
            try:
                file_date = datetime.strptime(ts_match.group(1), "%Y%m%d%H%M%S").date()
            except ValueError:
                file_date = today
        else:
            logger.warning("GCS: could not extract timestamp from filename — skipping: %s", blob.name)
            continue

        if file_date < cutoff:
            logger.debug("GCS skipped (older than %d days): %s", _SAFETY_NET_DAYS, blob.name)
            continue

        meta = _parse_filename(blob.name)
        meta.update(
            {
                "gcs_uri": f"gs://{config.GCS_BUCKET_NAME}/{blob.name}",
                "file_name": blob.name,
                "mime_type": _mime_type(blob.name),
                "uploaded_at": blob.updated.isoformat() if blob.updated else "",
                "duration": _calc_duration(blob.size),
            }
        )
        results.append(meta)
        logger.debug(
            "GCS queued: %s | from=%s to=%s date=%s",
            blob.name,
            meta["from"],
            meta["to"],
            meta["date"],
        )

    logger.info(
        "GCS: found %d recent 3CX audio files under '%s'.",
        len(results),
        _GCS_PREFIX,
    )
    return results


# ── Internal helpers ───────────────────────────────────────────────────────────

def _is_audio(blob_name: str) -> bool:
    ext = blob_name.lower().rsplit(".", 1)[-1]
    return ext in _MIME_MAP


def _mime_type(blob_name: str) -> str:
    ext = blob_name.lower().rsplit(".", 1)[-1]
    return _MIME_MAP.get(ext, "audio/mpeg")


def _parse_filename(blob_name: str) -> dict:
    """
    Extract structured metadata from the 3CX filename convention.

    Input examples (full blob path):
      Recordings/203/[Employee Name]_203-212_20260514103000.wav
      Recordings/203/[Employee Name]_203-+15550000000_20260514114500.mp3

    Returns a dict with keys: caller_name, from, to, date, duration.
    Falls back to safe defaults on parse failure so the pipeline never
    crashes on an unexpected filename.
    """
    # Work on the bare filename only, not the folder prefix
    basename = blob_name.rsplit("/", 1)[-1]
    m = _FILENAME_RE.match(basename)

    if m:
        name, from_ext, to_ext, timestamp, _ = m.groups()
        return {
            "caller_name": name.strip(),
            "from": from_ext,          # e.g. "203"
            "to": to_ext,              # e.g. "212" or "+15550000000"
            "date": _parse_date(timestamp),
            "time": _parse_time(timestamp),
            "duration": "N/A",         # overwritten by _calc_duration() in caller
        }

    logger.warning(
        "Could not parse 3CX filename — unexpected format: %s", blob_name
    )
    return {
        "caller_name": "Unknown",
        "from": "Unknown",
        "to": "Unknown",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "time": "",
        "duration": "N/A",
    }


def _calc_duration(blob_size: int | None) -> str:
    """
    Derive call duration from the blob size using known 3CX WAV encoding:
      8 kHz sample rate × 16-bit (2 bytes) × 1 channel = 16,000 bytes/second.
    No blob download or audio library required — pure arithmetic on metadata.
    """
    if not blob_size:
        return "N/A"
    total_seconds = int(blob_size / 16_000)
    return _fmt_mm_ss(total_seconds)


def _fmt_mm_ss(total_seconds: int) -> str:
    """Format an integer number of seconds as MM:SS (e.g. 125 → '02:05')."""
    m, s = divmod(total_seconds, 60)
    return f"{m:02d}:{s:02d}"


def _parse_date(timestamp: str) -> str:
    """Convert a 14-digit YYYYMMDDHHmmss timestamp to YYYY-MM-DD."""
    try:
        dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        logger.warning("Could not parse timestamp '%s' — using raw value.", timestamp)
        return timestamp


def _parse_time(timestamp: str) -> str:
    """Convert a 14-digit YYYYMMDDHHmmss UTC timestamp to local HH:MM AM/PM."""
    try:
        dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        dt += timedelta(hours=config.TIMEZONE_OFFSET)
        return dt.strftime("%I:%M %p")
    except ValueError:
        return ""
