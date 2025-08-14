# Docker команды для PoC RSS Loader

Полный набор команд для создания, удаления и управления контейнером PoC RSS Loader.

## Быстрый старт

### Сборка и запуск через Docker Compose (рекомендуется)
```bash
# Сборка образа и запуск
docker-compose up -d

# Остановка
docker-compose down

# Просмотр логов
docker-compose logs -f

# Перезапуск
docker-compose restart
```

## Docker команды

### Установка и сборка

#### Сборка образа
```bash
# Сборка образа
docker build -t poc-rss-loader .

# Сборка с тегом версии
docker build -t poc-rss-loader:v1.0 .

# Принудительная пересборка (без кеша)
docker build --no-cache -t poc-rss-loader .
```

#### Создание сети (если не используется docker-compose)
```bash
# Создание сети
docker network create poc-network

# Просмотр сетей
docker network ls

# Удаление сети
docker network rm poc-network
```

### Запуск контейнера

#### Базовый запуск
```bash
# Запуск в фоновом режиме
docker run -d --name poc-rss-loader --restart unless-stopped \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/poc_downloads:/app/poc_downloads \
  --network docker_default \
  poc-rss-loader
```

#### Запуск с кастомными параметрами
```bash
# Запуск с кастомным интервалом (10 минут)
docker run -d --name poc-rss-loader --restart unless-stopped \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/poc_downloads:/app/poc_downloads \
  --network docker_default \
  poc-rss-loader --interval 600

# Запуск в режиме однократной проверки
docker run -d --name poc-rss-loader --restart unless-stopped \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/poc_downloads:/app/poc_downloads \
  --network docker_default \
  poc-rss-loader --once

# Запуск с bootstrap режимом (50 элементов)
docker run -d --name poc-rss-loader --restart unless-stopped \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/poc_downloads:/app/poc_downloads \
  --network docker_default \
  poc-rss-loader --bootstrap-count 50
```

### Управление контейнером

#### Просмотр статуса
```bash
# Просмотр запущенных контейнеров
docker ps | grep poc-rss-loader

# Просмотр всех контейнеров (включая остановленные)
docker ps -a | grep poc-rss-loader

# Просмотр детальной информации
docker inspect poc-rss-loader
```

#### Управление жизненным циклом
```bash
# Остановка контейнера
docker stop poc-rss-loader

# Запуск остановленного контейнера
docker start poc-rss-loader

# Перезапуск контейнера
docker restart poc-rss-loader

# Пауза контейнера
docker pause poc-rss-loader

# Возобновление работы
docker unpause poc-rss-loader
```

#### Удаление
```bash
# Остановка и удаление контейнера
docker rm -f poc-rss-loader

# Удаление остановленного контейнера
docker rm poc-rss-loader

# Удаление образа
docker rmi poc-rss-loader

# Принудительное удаление образа
docker rmi -f poc-rss-loader
```

### Мониторинг и логи

#### Просмотр логов
```bash
# Просмотр всех логов
docker logs poc-rss-loader

# Просмотр последних N строк
docker logs --tail 100 poc-rss-loader

# Следить за логами в реальном времени
docker logs -f poc-rss-loader

# Логи с временными метками
docker logs -t poc-rss-loader

# Комбинация параметров
docker logs -f -t --tail 50 poc-rss-loader
```

#### Мониторинг ресурсов
```bash
# Просмотр использования ресурсов
docker stats poc-rss-loader

# Мониторинг в реальном времени
docker stats --no-stream poc-rss-loader

# Мониторинг всех контейнеров
docker stats
```

### Выполнение команд в контейнере

#### Интерактивный доступ
```bash
# Запуск bash в контейнере
docker exec -it poc-rss-loader bash

# Запуск с пользователем root
docker exec -it -u root poc-rss-loader bash
```

#### Выполнение команд
```bash
# Тестирование NVD API
docker exec poc-rss-loader python RSS_Linux.py --test-nvd

# Тестирование Git
docker exec poc-rss-loader python RSS_Linux.py --test-git

# Запуск в режиме однократной проверки
docker exec poc-rss-loader python RSS_Linux.py --once

# Проверка версии Python
docker exec poc-rss-loader python --version

# Проверка версии Git
docker exec poc-rss-loader git --version

# Просмотр содержимого директории
docker exec poc-rss-loader ls -la /app
```

### Управление томами и файлами

#### Просмотр примонтированных томов
```bash
# Просмотр информации о томах
docker inspect poc-rss-loader | grep -A 10 "Mounts"

# Просмотр содержимого логов
docker exec poc-rss-loader ls -la /app/logs

# Просмотр содержимого рабочей директории
docker exec poc-rss-loader ls -la /app/poc_downloads
```

#### Копирование файлов
```bash
# Копирование файла из контейнера на хост
docker cp poc-rss-loader:/app/logs/poc_loader.log ./logs/

# Копирование файла с хоста в контейнер
docker cp ./config.yml poc-rss-loader:/app/config.yml
```

### Отладка и диагностика

#### Проверка сетевого подключения
```bash
# Проверка подключения к OpenCTI
docker exec poc-rss-loader curl -I http://opencti:8080

# Проверка DNS разрешения
docker exec poc-rss-loader nslookup opencti

# Проверка сетевых интерфейсов
docker exec poc-rss-loader ip addr show
```

#### Проверка процессов
```bash
# Просмотр процессов в контейнере
docker exec poc-rss-loader ps aux

# Просмотр использования памяти
docker exec poc-rss-loader free -h

# Просмотр использования диска
docker exec poc-rss-loader df -h
```

### Docker Compose команды

#### Управление сервисами
```bash
# Запуск всех сервисов
docker-compose up -d

# Запуск конкретного сервиса
docker-compose up -d poc-rss-loader

# Остановка всех сервисов
docker-compose down

# Остановка конкретного сервиса
docker-compose stop poc-rss-loader

# Перезапуск сервиса
docker-compose restart poc-rss-loader
```

#### Логи и мониторинг
```bash
# Просмотр логов всех сервисов
docker-compose logs

# Просмотр логов конкретного сервиса
docker-compose logs poc-rss-loader

# Следить за логами в реальном времени
docker-compose logs -f poc-rss-loader

# Просмотр статуса сервисов
docker-compose ps
```

#### Пересборка и обновление
```bash
# Пересборка образа
docker-compose build

# Принудительная пересборка
docker-compose build --no-cache

# Пересборка и запуск
docker-compose up -d --build

# Обновление и перезапуск
docker-compose pull && docker-compose up -d
```

### Полезные команды для отладки

#### Полная очистка
```bash
# Остановка и удаление всех контейнеров
docker-compose down

# Удаление всех образов
docker system prune -a

# Удаление всех томов
docker volume prune

# Удаление всех сетей
docker network prune

# Полная очистка системы
docker system prune -a --volumes
```

#### Проверка состояния
```bash
# Проверка версии Docker
docker --version

# Проверка версии Docker Compose
docker-compose --version

# Проверка информации о системе
docker info

# Проверка дискового пространства
docker system df
```

## Примеры использования

### Типичный рабочий процесс
```bash
# 1. Сборка образа
docker-compose build

# 2. Запуск сервиса
docker-compose up -d

# 3. Проверка статуса
docker-compose ps

# 4. Просмотр логов
docker-compose logs -f poc-rss-loader

# 5. Остановка сервиса
docker-compose down
```

### Обновление конфигурации
```bash
# 1. Редактирование config.yml
nano config.yml

# 2. Перезапуск сервиса
docker-compose restart poc-rss-loader

# 3. Проверка применения изменений
docker-compose logs --tail 20 poc-rss-loader
```

### Отладка проблем
```bash
# 1. Проверка статуса
docker-compose ps

# 2. Просмотр логов
docker-compose logs poc-rss-loader

# 3. Вход в контейнер для диагностики
docker exec -it poc-rss-loader bash

# 4. Проверка сетевого подключения
docker exec poc-rss-loader curl -I http://opencti:8080
```
