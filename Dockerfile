FROM python:3.11-slim-bookworm

# System tools — ffmpeg, sox, mediainfo
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    sox \
    mediainfo \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Non-root user. HuggingFace Spaces requires uid 1000; Render runs any uid, so the
# same image works on both.
RUN useradd -m -u 1000 alfred
WORKDIR /home/alfred/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=alfred:alfred . .

USER alfred

# Informational only: the app binds whatever the platform asks for — $PORT on Render,
# defaulting to 7860 (HuggingFace Spaces' documented port) when nothing is injected.
# EXPOSE is not what makes the port reachable on either platform.
EXPOSE 7860

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]

