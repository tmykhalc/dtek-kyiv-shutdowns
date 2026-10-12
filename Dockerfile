FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8081

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

# System tools we still like to have
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl dumb-init tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY scrape_dtek_playwright.py /app/scrape_dtek_playwright.py
COPY service.py /app/service.py

RUN useradd -ms /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE ${PORT}
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
  CMD curl -fsS http://localhost:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["sh", "-c", "exec uvicorn service:app --host 0.0.0.0 --port \"$PORT\""]

