FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by the compose healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Mount points for state that must outlive an image rebuild.
#   /app/data          database + saved SMTP/Paystack config
#   /app/static/uploads property images (served by Flask's /static route)
#   /app/uploads       tenant documents (served via send_file)
RUN mkdir -p /app/data /app/static/uploads/properties /app/uploads/documents

# Run as a non-root user; the volumes are chowned to this uid by the deploy script.
RUN useradd --create-home --uid 10001 hsapp \
 && chown -R hsapp:hsapp /app
USER hsapp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/ >/dev/null || exit 1

# 3 workers is comfortable for this app; the scheduler runs in its own container
# so no worker ever owns the cron jobs.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--workers", "3", "--threads", "2", \
     "--timeout", "120", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "wsgi:app"]
