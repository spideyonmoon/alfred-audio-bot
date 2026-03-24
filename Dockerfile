FROM python:3.11-slim-bookworm

# System tools — ffmpeg, sox, mediainfo
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    sox \
    mediainfo \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# HuggingFace Spaces run as a non-root user (uid 1000)
RUN useradd -m -u 1000 alfred
WORKDIR /home/alfred/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=alfred:alfred . .

USER alfred

# Port 7860 is the expected HuggingFace Spaces health port
EXPOSE 7860

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]

