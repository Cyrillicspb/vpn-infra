ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# Хэш последнего коммита home/telegram-bot/ — для надёжной детекции изменений в deploy.sh
ARG GIT_HASH=unknown
LABEL git-hash=$GIT_HASH

WORKDIR /app

# Python зависимости
COPY requirements.txt ./
COPY wheels/ ./wheels/
RUN if find /app/wheels -maxdepth 1 -type f -name '*.whl' | grep -q .; then \
        pip install --no-cache-dir --no-index --find-links /app/wheels -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt --timeout 120 --index-url https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn || \
        pip install --no-cache-dir -r requirements.txt --timeout 120; \
    fi

# Код приложения
COPY . .

# Директория для SQLite базы
RUN mkdir -p /app/data

# Запуск от непривилегированного пользователя
RUN useradd -r --uid 999 -s /sbin/nologin botuser \
    && chown -R botuser:botuser /app
USER botuser

CMD ["python", "bot.py"]
