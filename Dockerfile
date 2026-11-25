FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DUMB_SPEAKER_PORT=8765

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download default English and Ukrainian Piper voices into /app/voices
ARG PIPER_VOICES="en_US-lessac-medium uk_UA-mykyta-high"
RUN mkdir -p /app/voices /app/media && \
    python -m piper.download_voices --download-dir /app/voices ${PIPER_VOICES}

COPY speaker.py speaker_manager.py server.py tts.py pomodoro.py ./
COPY static ./static

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"DUMB_SPEAKER_PORT\", \"8765\")}/health', timeout=3)" || exit 1

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${DUMB_SPEAKER_PORT:-8765}"]
