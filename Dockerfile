# ── Build stage ────────────────────────────────────────────────────────────────
# Using slim to minimise image size; Python 3.12 is fully supported by all deps.
FROM python:3.12-slim AS base

# Prevents Python from writing .pyc files and buffering stdout/stderr —
# both are important for Cloud Run Jobs where logs are streamed to Cloud Logging.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ── Dependencies ───────────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Application code ───────────────────────────────────────────────────────────
COPY config.py .
COPY main.py .
COPY services/ ./services/

# The service account key is injected at runtime via a Secret Manager volume
# mount in the Cloud Run Job definition — never baked into the image.
# GOOGLE_APPLICATION_CREDENTIALS is set in the job's environment variables.

# ── Entry point ────────────────────────────────────────────────────────────────
# Cloud Run Jobs: the container must exit 0 on success.
# Cloud Run will mark the job execution as failed if the exit code is non-zero.
CMD ["python", "main.py"]
