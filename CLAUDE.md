# Meeting Transcript Intelligence Bot - Architecture Guidelines

## Core Infrastructure
* **Deployment:** Google Cloud Run Jobs (Serverless Batch).
* **Language:** Python 3.10+.
* **Environment:** Use `.env` for all configuration. Never hardcode credentials, emails, or bucket names.

## STRICT AI Rules (CRITICAL)
* **Library:** You MUST use the modern `google-genai` library. NEVER use the legacy `vertexai` library.
* **Initialization:** Always initialize with enterprise credentials: `client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)`.
* **Routing Logic:** Always maintain the two-tier triage system. Default to `gemini-2.5-flash`. Only route to `gemini-2.5-pro` if Flash explicitly flags the sentiment as Negative/Escalation.

## Integration Rules
* **Google Workspace:** Use Domain-Wide Delegation (DWD) to impersonate the admin for fetching Meet logs. 
* **Google Drive:** READ ONLY. Never attempt to mutate file permissions.
* **Google Cloud Storage (3CX):** Stream or pass GCS URIs directly to the AI model. Do not download large audio files to the local container memory.
* **Google Sheets:** Read targets from the `Config` tab. Append processed data ONLY to the `Database` tab.
* **Email Routing:** Department summaries go to individual managers. The Master Consolidated Report goes directly to the Admin/Executive. Do not CC the Admin on individual department emails.