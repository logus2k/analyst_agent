# Analyst Agent — no docling/torch here: document parsing is delegated to the
# shared ingestion-server (:8700), which keeps this image small.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7803

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY knowledge/ /app/knowledge/
ENV PYTHONPATH=/app/src \
    ANALYST_KNOWLEDGE=/app/knowledge \
    ANALYST_STORE=/app/store

EXPOSE 7803
CMD ["uvicorn", "analyst_agent.api:asgi", "--host", "0.0.0.0", "--port", "7803"]
