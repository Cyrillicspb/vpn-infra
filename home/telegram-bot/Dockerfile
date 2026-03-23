FROM python:3.12-slim

# Хэш последнего коммита home/telegram-bot/ — для надёжной детекции изменений в deploy.sh
ARG GIT_HASH=unknown
LABEL git-hash=$GIT_HASH

WORKDIR /app

# Системные зависимости: wireguard-tools для wg genkey/pubkey
RUN apt-get update -qq && apt-get install -y -qq \
    curl \
    wireguard-tools \
    && rm -rf /var/lib/apt/lists/*

# Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY . .

# Директория для SQLite базы
RUN mkdir -p /app/data

# Запуск от непривилегированного пользователя
RUN useradd -r --uid 999 -s /sbin/nologin botuser \
    && chown -R botuser:botuser /app
USER botuser

CMD ["python", "bot.py"]
