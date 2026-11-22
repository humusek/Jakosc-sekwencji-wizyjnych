FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 1 proces + watki: /api/encoders i /api/probe odpowiadaja nawet w trakcie kodowania;
# kodowania i tak wykonuja sie pojedynczo (blokada w app.py), wiec czasy sa porownywalne.
CMD ["sh", "-c", "gunicorn -w 1 --threads 4 -t 900 -b 0.0.0.0:${PORT:-8000} app:app"]
