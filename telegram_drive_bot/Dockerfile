FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ca-certificates \
    ffmpeg \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY telegram_drive_bot/requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-u", "telegram_drive_bot/bot.py"]
