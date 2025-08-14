FROM python:3.11-slim

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    git \
    tar \
    gzip \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Создаем рабочую директорию
WORKDIR /app

# Копируем файлы зависимостей
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем основной скрипт и конфигурацию
COPY RSS_Linux.py .
COPY config.yml .

# Создаем директории для работы
RUN mkdir -p /app/poc_downloads /app/logs

# Устанавливаем переменные окружения
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Точка входа
ENTRYPOINT ["python", "RSS_Linux.py"]
