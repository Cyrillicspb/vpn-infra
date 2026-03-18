FROM python:3.13-slim

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
RUN useradd -r -s /sbin/nologin botuser \
    && chown -R botuser:botuser /app
USER botuser

CMD ["python", "bot.py"]
