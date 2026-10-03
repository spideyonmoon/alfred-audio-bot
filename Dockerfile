FROM python:3.11-slim-bookworm

# System tools — ffmpeg, sox, mediainfo
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    sox \
    mediainfo \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Run the bot as a dedicated unprivileged user.
RUN useradd -m alfred
WORKDIR /home/alfred/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=alfred:alfred . .

RUN mkdir -p data && chown alfred:alfred data

USER alfred

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]

