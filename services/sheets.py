"""
Google Sheets integration.

Two operations only:
  read_config()        — reads the 'Config' tab (never writes to it)
  append_to_database() — appends one row per meeting to the 'Database' tab

Config tab expected columns (row 1 = header, data from row 2):
  A: Employee Email | B: Department | C: Process Meetings? | D: Manager Email | E: Extension

Database tab columns (appended):
  A: Date | B: Department | C: From | D: To | E: Duration |
  F: Sentiment | G: Summary | H: Action Points | I: Full Transcript/Audio Link
"""
import logging

from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)

# Config tab column indices (0-based after Sheets returns a list)
_C_EMAIL = 0
_C_DEPT = 1
_C_PROCESS = 2
_C_MANAGER = 3
_C_EXT = 4  # 3CX internal extension number (e.g. "223") — optional per row


def read_config(sheets_service) -> list[dict]:
    """
    Return a list of dicts for every user whose 'Process Meetings?' cell is 'Yes'
    (case-insensitive).
    """
    try:
        result = (
            sheets_service.spreadsheets()
            .values()
            .get(
                spreadsheetId=config.SPREADSHEET_ID,
                range=f"{config.CONFIG_TAB}!A2:E",
            )
            .execute()
        )
    except HttpError as exc:
        logger.error("Failed to read Config tab: %s", exc)
        return []

    rows = result.get("values", [])
    users: list[dict] = []

    for row in rows:
        # Tolerate short rows (missing trailing columns)
        if len(row) < 4:
            continue
        if row[_C_PROCESS].strip().lower() != "yes":
            continue
        users.append(
            {
                "email": row[_C_EMAIL].strip().lower(),
                "department": row[_C_DEPT].strip(),
                "manager_email": row[_C_MANAGER].strip(),
                # Extension is optional — blank string means no 3CX line assigned.
                # Sheets sometimes serialises numeric cells as "223.0"; _clean_ext
                # strips whitespace and removes any trailing ".0" artefact.
                "extension": _clean_ext(row[_C_EXT]) if len(row) > _C_EXT else "",
            }
        )

    logger.info("Config: %d users enabled for processing.", len(users))
    return users


def _clean_ext(raw: str) -> str:
    """
    Normalise a 3CX extension value read from Google Sheets.
    Sheets serialises unformatted numeric cells as floats (e.g. "223.0");
    this strips whitespace then removes any trailing ".0" so the result
    always matches the plain integer string extracted from the filename.
    """
    value = raw.strip()
    if value.endswith(".0"):
        value = value[:-2]
    return value


def fetch_processed_ids(sheets_service) -> set[str]:
    """
    Return the set of transcript_link values (column I) already written to the
    Database tab. Used before calling the AI so already-processed calls are
    skipped without incurring model cost.
    """
    try:
        result = (
            sheets_service.spreadsheets()
            .values()
            .get(
                spreadsheetId=config.SPREADSHEET_ID,
                range=f"{config.DATABASE_TAB}!I2:I",
            )
            .execute()
        )
    except HttpError as exc:
        logger.warning("Could not read Database tab for deduplication: %s", exc)
        return set()

    rows = result.get("values", [])
    return {row[0].strip() for row in rows if row and row[0].strip()}


def append_to_database(sheets_service, record: dict) -> None:
    """
    Append a single processed meeting/call record to the Database tab.
    Action Points list is joined as newline-separated text for readability.
    """
    action_points_text = "\n".join(record.get("action_points", []))

    row_values = [
        [
            record.get("date", ""),
            record.get("department", ""),
            record.get("from", ""),
            record.get("to", ""),
            record.get("duration", ""),
            record.get("sentiment", ""),
            record.get("summary", ""),
            action_points_text,
            record.get("transcript_link", ""),
        ]
    ]

    try:
        (
            sheets_service.spreadsheets()
            .values()
            .append(
                spreadsheetId=config.SPREADSHEET_ID,
                range=f"{config.DATABASE_TAB}!A:I",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": row_values},
            )
            .execute()
        )
        logger.debug(
            "Database: appended row for %s / %s.",
            record.get("department"),
            record.get("date"),
        )
    except HttpError as exc:
        logger.error(
            "Failed to append Database row (dept=%s): %s",
            record.get("department"),
            exc,
        )
