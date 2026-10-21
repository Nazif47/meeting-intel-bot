"""
3CX V20 REST API — contact lookup via OAuth2 client credentials.

Auth flow:
  1. POST {API_URL}/connect/token  (client_credentials grant)
     using TCX_CLIENT_ID + TCX_CLIENT_SECRET from .env
  2. Received access_token is used as Bearer for all subsequent calls.

Two-tier lookup strategy:
  Tier 1 — /xapi/v1/contacts  (external numbers: mobile, landline, etc.)
  Tier 2 — /xapi/v1/users     (internal extensions: 206, 212, 224, …)

Both tiers are tried in order; the first match wins.  All network errors,
4xx responses, and JSON parse failures are caught so the pipeline never
crashes — an empty {"name": None, "company": None} is returned instead.
"""
import logging
import os
import re

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_EMPTY = {"name": None, "company": None}

# Module-level token cache — fetched once per job run, reused for all lookups.
_cached_token: str | None = None

# Strips spaces, dashes, parentheses, and + before numeric comparison
_NORMALISE_RE = re.compile(r"[\s\-()+]")

# Short numeric strings that look like internal PBX extensions (≤ 6 digits)
_EXTENSION_RE = re.compile(r"^\d{1,6}$")


# ── Config helpers ─────────────────────────────────────────────────────────────

def _api_url() -> str:
    load_dotenv()
    return os.getenv("3CX_API_URL", "").strip().rstrip("/")


def _client_id() -> str:
    load_dotenv()
    return os.getenv("TCX_CLIENT_ID", "").strip(' "\'')


def _client_secret() -> str:
    load_dotenv()
    return os.getenv("TCX_CLIENT_SECRET", "").strip(' "\'')


# ── OAuth2 token exchange ──────────────────────────────────────────────────────

def _get_access_token() -> str | None:
    """
    Exchange client credentials for a bearer token.
    Result is cached for the lifetime of the process (one job run).
    Returns None on any failure so callers can skip gracefully.
    """
    global _cached_token
    if _cached_token:
        return _cached_token

    base   = _api_url()
    cid    = _client_id()
    secret = _client_secret()

    if not base:
        logger.error("3CX: 3CX_API_URL is not set — contact enrichment disabled.")
        return None
    if not cid or not secret:
        logger.error(
            "3CX: TCX_CLIENT_ID or TCX_CLIENT_SECRET is missing — "
            "contact enrichment disabled. Check your .env file."
        )
        return None

    token_url = f"{base}/connect/token"
    logger.info("3CX OAuth2: requesting token from %s (client_id=%s).", token_url, cid)

    try:
        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": secret,
            },
            timeout=10,
        )
    except Exception as exc:
        logger.error("3CX OAuth2: token request raised an exception: %s", exc)
        return None

    if not resp.ok:
        logger.error(
            "3CX OAuth2: token exchange failed — %s %s | body: %.400s",
            resp.status_code, resp.reason, resp.text,
        )
        return None

    try:
        token = resp.json().get("access_token")
    except Exception as exc:
        logger.error("3CX OAuth2: could not parse token response: %s | body: %.400s", exc, resp.text)
        return None

    if not token:
        logger.error("3CX OAuth2: response contained no access_token | body: %.400s", resp.text)
        return None

    logger.info(
        "3CX OAuth2: token acquired | length=%d | starts_with='%s...'",
        len(token), token[:4],
    )
    _cached_token = token
    return token


# ── HTTP helper ────────────────────────────────────────────────────────────────

def _get_json(endpoint: str, token: str) -> list | dict | None:
    """
    GET *endpoint* with the provided bearer token.
    Returns parsed JSON body, or None on any error.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(endpoint, headers=headers, timeout=10)
        if not resp.ok:
            logger.warning(
                "3CX API %s returned %s %s.", endpoint, resp.status_code, resp.reason
            )
            return None
        return resp.json()
    except Exception as exc:
        logger.warning("3CX API request failed (%s): %s", endpoint, exc)
        return None


# ── Number helpers ─────────────────────────────────────────────────────────────

def _normalise(number: str) -> str:
    """Strip formatting chars so '(+1) 555-0000000' == '15550000000'."""
    return _NORMALISE_RE.sub("", number)


def _extract_numbers(contact: dict) -> list[str]:
    """Collect every phone number value present on a contact/user record."""
    numbers: list[str] = []
    for field in ("Number", "PhoneNumber", "Mobile", "HomePhone",
                  "BusinessPhone", "OtherPhone", "Extension"):
        val = contact.get(field)
        if val:
            numbers.append(str(val))
    for entry in contact.get("Numbers", []):
        if isinstance(entry, dict):
            val = entry.get("Number") or entry.get("number")
            if val:
                numbers.append(str(val))
        elif isinstance(entry, str):
            numbers.append(entry)
    return numbers


def _match_contact(contacts: list, needle: str) -> dict | None:
    """
    Return the first contact whose normalised number matches *needle*,
    or None if no match is found.
    """
    for contact in contacts:
        if any(_normalise(n) == needle for n in _extract_numbers(contact)):
            first   = (contact.get("FirstName") or "").strip()
            last    = (contact.get("LastName")  or "").strip()
            company = (
                contact.get("CompanyName")
                or contact.get("Company")
                or ""
            ).strip()
            name = f"{first} {last}".strip() or None
            return {"name": name or None, "company": company or None}
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def lookup_contact(phone_number: str) -> dict:
    """
    Look up a phone number against the 3CX V20 contact directory.

    Returns {"name": str|None, "company": str|None}.
    Always safe to call — returns {"name": None, "company": None} when the
    API is not configured, the number is blank/unknown, or any error occurs.
    """
    if not phone_number or phone_number.strip() in ("", "Unknown"):
        return _EMPTY

    token = _get_access_token()
    if not token:
        return _EMPTY

    needle = _normalise(phone_number.strip())
    base   = _api_url()

    # ── Tier 1: external contacts (/xapi/v1/contacts) ──────────────────────
    data = _get_json(f"{base}/xapi/v1/contacts", token)
    if data is not None:
        contacts = data.get("value", data) if isinstance(data, dict) else data
        if isinstance(contacts, list):
            result = _match_contact(contacts, needle)
            if result:
                logger.debug("3CX contacts match for %s: %s", phone_number, result["name"])
                return result

    # ── Tier 2: internal users/extensions (/xapi/v1/users) ────────────────
    # Only attempt for short numeric strings that look like PBX extensions.
    if _EXTENSION_RE.match(needle):
        data = _get_json(f"{base}/xapi/v1/users", token)
        if data is not None:
            users = data.get("value", data) if isinstance(data, dict) else data
            if isinstance(users, list):
                result = _match_contact(users, needle)
                if result:
                    logger.debug("3CX users match for %s: %s", phone_number, result["name"])
                    return result

    logger.debug("3CX: no match found for %s.", phone_number)
    return _EMPTY
