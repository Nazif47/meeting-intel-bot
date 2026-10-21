"""
AI processing service — google-genai library only (CLAUDE.md: NEVER use vertexai).

Model routing strategy:
  process_transcript (Google Meet) → COMPLEX_MODEL directly.
    Meet transcripts are long-form, multi-speaker, and benefit from the
    higher-capability model on every call — no Flash pre-screening needed.

  process_audio (3CX) → DEFAULT_MODEL first (Flash), then COMPLEX_MODEL
    only if sentiment is Negative / Angry.  Short audio clips rarely need
    the complex model; this keeps cost and latency low for the common case.

GCS audio is passed as a gs:// URI — the model streams it directly.
No audio bytes are ever loaded into container memory.
"""
import json
import logging
import re

from google import genai
from google.genai import types

import config

logger = logging.getLogger(__name__)

# Lazy singleton — created once, reused across all calls in the job run.
_client: genai.Client | None = None

# ── Prompts ────────────────────────────────────────────────────────────────────

_SYSTEM_INSTRUCTION = """\
You are an expert multilingual call-centre analyst.

The audio or text you receive may be in English, Hindi, Malayalam, Gujarati,
Arabic, or Tamil. Auto-detect the language(s), translate all content to English,
and produce ONLY a single valid JSON object — no markdown fences, no commentary.

Required JSON schema (all six fields mandatory):
{
  "Sentiment":              "<one of: Positive | Neutral | Negative | Angry>",
  "Summary":                "<concise English summary of the conversation>",
  "Action Points":          ["<action item 1>", "<action item 2>"],
  "full_transcript":        "<complete word-for-word transcript, translated to English>",
  "is_actual_conversation": <true | false>
}

Field rules:
- Sentiment: exactly one of Positive, Neutral, Negative, Angry.
- Action Points: JSON array of strings; empty array [] if none.
- full_transcript: verbatim transcript of what was said, in English.
- is_actual_conversation: boolean (no quotes).
    true  — a real two-way conversation took place.
    false — the call was unanswered, went to voicemail, was just ringing,
            or the agent only spoke with an automated IVR / receptionist
            without reaching the intended person.
- All string values must be in English.
- Output must be valid JSON with exactly the five keys above.
"""

_TRANSCRIPT_USER_MSG = "Analyse the following meeting transcript:\n\n{text}"
_AUDIO_USER_MSG = (
    "Analyse this call recording. Transcribe it, translate to English if needed, "
    "then provide the structured analysis."
)


# ── Public API ─────────────────────────────────────────────────────────────────

def process_transcript(transcript_text: str, model: str | None = None) -> dict:
    """
    Analyse a plain-text Google Meet transcript.

    Routes directly to COMPLEX_MODEL (no Flash pre-screening) because Meet
    transcripts are long-form and always benefit from the higher-capability
    model.  Pass *model* to override for testing or future routing changes.
    """
    target = model or config.COMPLEX_MODEL
    parts  = [types.Part(text=_TRANSCRIPT_USER_MSG.format(text=transcript_text))]
    client = _get_client()
    result = _call_model(client, target, parts)
    result.setdefault("_escalated", False)
    return result


def process_audio(gcs_uri: str, mime_type: str = "audio/mpeg",
                  model: str | None = None) -> dict:
    """
    Analyse a 3CX audio file.  The GCS URI is passed directly to the model —
    no local download.

    Routes through the two-tier path: DEFAULT_MODEL first, then COMPLEX_MODEL
    only if sentiment is Negative / Angry.  Pass *model* to pin a specific
    model and bypass the escalation logic entirely.
    """
    parts = [
        types.Part.from_uri(file_uri=gcs_uri, mime_type=mime_type),
        types.Part(text=_AUDIO_USER_MSG),
    ]
    if model:
        client = _get_client()
        result = _call_model(client, model, parts)
        result.setdefault("_escalated", False)
        return result
    return _two_tier(parts)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_client() -> genai.Client:
    global _client
    if _client is None:
        # Enterprise Vertex AI initialisation — as mandated by CLAUDE.md
        _client = genai.Client(
            vertexai=True,
            project=config.PROJECT_ID,
            location=config.LOCATION,
        )
        logger.info(
            "genai.Client initialised (Vertex AI, project=%s, location=%s).",
            config.PROJECT_ID,
            config.LOCATION,
        )
    return _client


def _two_tier(user_parts: list[types.Part]) -> dict:
    """
    3CX audio escalation path.
    Tier 1: DEFAULT_MODEL (Flash) — fast triage for all audio.
    Tier 2: COMPLEX_MODEL (Pro)   — full re-process on Negative / Angry.
    Returns the final dict augmented with _escalated bool.
    """
    client = _get_client()

    flash_result = _call_model(client, config.DEFAULT_MODEL, user_parts)

    if flash_result.get("Sentiment") in config.ESCALATION_SENTIMENTS:
        logger.info(
            "Sentiment '%s' — escalating to %s for deep-dive analysis.",
            flash_result.get("Sentiment"),
            config.COMPLEX_MODEL,
        )
        pro_result = _call_model(client, config.COMPLEX_MODEL, user_parts)
        pro_result["_escalated"] = True
        return pro_result

    flash_result["_escalated"] = False
    return flash_result


def _call_model(
    client: genai.Client, model: str, user_parts: list[types.Part]
) -> dict:
    """
    Call the specified Gemini model and return a parsed dict.
    Enforces JSON response mode and strips any accidental markdown wrapping.
    Falls back to a safe default dict on any error so the pipeline continues.
    """
    _FALLBACK = {
        "Sentiment": "Neutral",
        "Summary": "AI processing encountered an error — manual review required.",
        "Action Points": [],
        "full_transcript": "",
        "is_actual_conversation": True,
        "_error": True,
    }

    try:
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Content(role="user", parts=user_parts),
            ],
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=65536,
            ),
        )
        raw = response.text.strip()
    except Exception as exc:
        logger.error("Model %s API call failed: %s", model, exc)
        return _FALLBACK

    # Strip accidental markdown code fences the model may add despite instructions
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
        logger.debug("Model %s returned sentiment: %s", model, result.get("Sentiment"))
        return result
    except json.JSONDecodeError as exc:
        logger.warning(
            "Model %s returned truncated/invalid JSON — using fallback. "
            "Error: %s | raw (first 300 chars): %.300s",
            model, exc, raw,
        )
        return _FALLBACK
