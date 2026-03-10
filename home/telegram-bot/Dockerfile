FROM python:3.12-slim

WORKDIR /app

# Зависимости системы
RUN apt-get update -qq && apt-get install -y -qq \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY . .

# Директория для данных (SQLite)
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
