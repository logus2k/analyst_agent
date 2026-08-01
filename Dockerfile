# Analyst Agent — no docling/torch here: document parsing is delegated to the
# shared ingestion-server (:8700), which keeps this image small.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7803

WORKDIR /app

# System libraries WeasyPrint needs at runtime for the reissue PDF (pango/fontconfig +
# a base font). pip cannot provide these — they must be present in the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfribidi0 \
        libffi8 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY knowledge/ /app/knowledge/
ENV PYTHONPATH=/app/src \
    ANALYST_KNOWLEDGE=/app/knowledge \
    ANALYST_STORE=/app/store

EXPOSE 7803
CMD ["uvicorn", "analyst_agent.api:asgi", "--host", "0.0.0.0", "--port", "7803"]
