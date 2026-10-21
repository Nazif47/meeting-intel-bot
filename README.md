# Meeting Intelligence Bot

A cloud-native Python pipeline that turns raw business conversations into actionable, department-level intelligence. It ingests **VoIP call recordings (3CX)** and **Google Meet transcripts**, analyses them with the **Gemini API on Google Vertex AI**, and delivers automated call reports with sentiment analysis, summaries, and action points to the right people every day.

It runs as a serverless batch job on **Google Cloud Run Jobs**: it wakes on a schedule, processes everything new since the last run, logs results to Google Sheets, emails PDF reports, and exits.

---

## Architecture

```
                    ┌──────────────────────────┐
                    │  Cloud Scheduler (cron)  │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │   Cloud Run Job (Python) │
                    └────────────┬─────────────┘
          ┌──────────────────────┼───────────────────────┐
          ▼                      ▼                       ▼
 ┌─────────────────┐   ┌──────────────────┐   ┌────────────────────┐
 │ Google Sheets   │   │ Workflow A:      │   │ Workflow B:        │
 │ "Config" tab    │   │ Google Meet      │   │ 3CX recordings     │
 │ (targets, depts,│   │ Admin SDK logs → │   │ in Cloud Storage   │
 │  managers)      │   │ Drive transcripts│   │ (GCS URIs)         │
 └─────────────────┘   └────────┬─────────┘   └─────────┬──────────┘
                                └──────────┬────────────┘
                                           ▼
                         ┌───────────────────────────────────┐
                         │ Gemini (Vertex AI) two-tier triage│
                         │  Flash → Pro on negative sentiment│
                         └─────────────────┬─────────────────┘
                                           ▼
          ┌──────────────────────┬─────────────────────┬──────────────────────┐
          ▼                      ▼                     ▼                      ▼
 ┌─────────────────┐  ┌────────────────────┐  ┌─────────────────┐  ┌───────────────────┐
 │ Sheets          │  │ 3CX V20 API        │  │ PDF reports     │  │ Gmail API         │
 │ "Database" tab  │  │ contact enrichment │  │ per department  │  │ manager + master  │
 │ (append-only)   │  │ (optional)         │  │                 │  │ email routing     │
 └─────────────────┘  └────────────────────┘  └─────────────────┘  └───────────────────┘
```

### Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Compute | Google Cloud Run Jobs (serverless batch), Docker |
| Storage | Google Cloud Storage (3CX call recordings) |
| AI | Gemini API via Vertex AI, using the [`google-genai`](https://pypi.org/project/google-genai/) SDK |
| Workspace | Admin SDK Reports API (Meet activity), Drive API (read-only transcripts), Sheets API, Gmail API, all via **Domain-Wide Delegation** |
| Telephony | 3CX V20 API (OAuth2 client credentials) for contact lookup |
| Reporting | `fpdf2` for PDF generation |
| Config | `python-dotenv` / Cloud Run environment variables |

---

## Key Features

- **Automated audio transcription and analysis.** Call recordings are passed to Gemini as GCS URIs. Audio is never downloaded into the container, which keeps memory use flat no matter how large the files are.
- **Multi-tier LLM routing.** Every conversation goes to a fast, low-cost model first (Gemini Flash). Only conversations it flags as **Negative / Escalation** are re-analysed by the larger model (Gemini Pro), so deep analysis is spent where it matters.
- **Google Meet ingestion.** Meet sessions are found through the Admin SDK Reports API, and their transcripts are read from Drive by impersonating each host through Domain-Wide Delegation. Drive access is strictly read-only.
- **Robust 3CX filename parsing.** A regex parser extracts caller name, extension, dialled number (including international and URL-encoded formats), and timestamp from 3CX recording paths.
- **Sheets-driven configuration.** Target users, extensions, departments, and managers are kept in a Google Sheet `Config` tab, so non-engineers can change scope without a redeploy.
- **Automated Google Sheets database updates.** Every processed call or meeting is appended to a `Database` tab. That tab also handles deduplication, so re-runs never process the same item twice.
- **Contact enrichment.** Optionally resolves external numbers to contact and company names through the 3CX V20 API.
- **Department PDF reports.** Records are grouped Date → Agent → Calls, with sentiment colour-coding, summaries, action points, and full transcripts.
- **Email dispatch routing.**
  - Each department manager gets **only their department's** report.
  - The executive gets **one consolidated master report** and is never CC'd on department emails.
  - A configurable **executive privacy filter** routes sensitive calls to an isolated report that never reaches mid-level managers.

---

## Project Structure

```
.
├── main.py              # Orchestration: config → ingest → AI → Sheets → PDF → email
├── config.py            # Centralised, env-driven configuration
├── services/
│   ├── auth.py          # Service account + Domain-Wide Delegation clients
│   ├── ai.py            # google-genai client, two-tier Flash → Pro triage
│   ├── drive.py         # Meet activity logs + Drive transcript retrieval
│   ├── gcs.py           # 3CX recording discovery + filename parsing
│   ├── tcx_api.py       # 3CX V20 API contact enrichment
│   ├── sheets.py        # Config read, Database append, deduplication
│   ├── pdf.py           # Department PDF report generation
│   └── email.py         # Gmail dispatch + routing rules
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Setup & Installation

### 1. Prerequisites

- Python 3.10+
- A Google Cloud project with these APIs enabled: **Vertex AI**, **Cloud Storage**, **Admin SDK**, **Google Drive**, **Google Sheets**, **Gmail**
- A Google Workspace super-admin account (for Domain-Wide Delegation)
- *(Optional)* 3CX V20 API client ID and secret

### 2. Clone and create a virtual environment

```bash
git clone https://github.com/<your-username>/meeting-intel-bot.git
cd meeting-intel-bot

python -m venv venv
# macOS / Linux
source venv/bin/activate
# Windows
venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Configure the environment

```bash
cp .env.example .env
```

Fill in `.env` with your own values. The key settings:

| Variable | Purpose |
|---|---|
| `GCP_PROJECT_ID`, `GCP_LOCATION` | Vertex AI project and region |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to the service account key |
| `ADMIN_EMAIL` | Workspace admin impersonated through DWD |
| `GCS_BUCKET_NAME` | Bucket holding 3CX recordings |
| `SPREADSHEET_ID` | Sheet containing the `Config` and `Database` tabs |
| `EMAIL_SENDER`, `MASTER_REPORT_RECIPIENT` | Sender and executive report recipient |
| `EXECUTIVE_IDENTIFIERS` | Extensions, numbers, or emails routed to the private executive report |
| `PRIMARY_AI_MODEL`, `ESCALATION_AI_MODEL` | Optional model overrides |

> `.env` and all service account key files are git-ignored. Never commit credentials.

### 4. Authenticate the GCP service account

1. In **IAM & Admin → Service Accounts**, create a service account and download a JSON key as `service_account.json`.
2. Grant it **Vertex AI User** and **Storage Object Viewer** on the project or bucket.
3. In the **Google Workspace Admin Console → Security → API controls → Domain-wide delegation**, authorise the service account's client ID with these scopes:
   ```
   https://www.googleapis.com/auth/admin.reports.audit.readonly
   https://www.googleapis.com/auth/drive.readonly
   https://www.googleapis.com/auth/spreadsheets
   https://www.googleapis.com/auth/gmail.send
   ```
4. Share the Google Sheet with the impersonated admin account.
5. Point `GOOGLE_APPLICATION_CREDENTIALS` at the key file.

### 5. Run locally

```bash
python main.py
```

---

## Deploying to Cloud Run Jobs

```bash
# Build and push the image
gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT_ID/REPO/meeting-intel-bot

# Create the job (store the service account key in Secret Manager and mount it)
gcloud run jobs create meeting-intel-bot \
  --image REGION-docker.pkg.dev/PROJECT_ID/REPO/meeting-intel-bot \
  --region REGION \
  --set-secrets /app/service_account.json=sa-key:latest \
  --env-vars-file env.yaml

# Schedule a daily run with Cloud Scheduler, or trigger it manually:
gcloud run jobs execute meeting-intel-bot --region REGION
```

The service account key is mounted at runtime from Secret Manager and is never baked into the image.

---

## Security Notes

- No credentials, emails, or bucket names are hard-coded; all configuration comes from the environment.
- Drive access is read-only, and the bot never changes file permissions.
- Audio is streamed by URI to the model and never stored on the container.
- Privacy routing keeps executive-level conversations out of manager-level reports.
