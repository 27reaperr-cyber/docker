# 🤖 TG Hosting Bot

Telegram-бот для хостинга Git-проектов (GitHub / GitLab) прямо с вашего VPS.  
Оптимизирован для **[Bothost.ru](https://bothost.ru)**.

---

## 📁 Структура проекта

```
tg-hosting/
├── bot.py               # Основной файл бота (aiogram 3.x, FSM, polling)
├── database.py          # SQLite-слой (aiosqlite)
├── deploy.py            # Пайплайн деплоя (git clone → pip install → docker run)
├── process_manager.py   # Управление Docker-контейнерами
├── requirements.txt     # Python-зависимости
├── Dockerfile           # Образ для запуска бота
├── docker-compose.yml   # Удобный запуск через compose
├── .env.example         # Пример переменных окружения
├── projects/            # Клонированные репозитории (user_id/project_id/)
└── logs/                # Файловые логи проектов
```

---

## ⚡ Быстрый старт на Bothost.ru

### 1. Подключитесь к VPS по SSH

```bash
ssh root@<ваш_ip>
```

### 2. Установите необходимые пакеты

```bash
# Обновление системы
apt-get update && apt-get upgrade -y

# Git, curl, Python 3.10+
apt-get install -y git curl python3 python3-pip python3-venv

# Docker (официальный скрипт)
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker

# Docker Compose
apt-get install -y docker-compose-plugin
# Проверка
docker compose version
```

### 3. Клонируйте проект на сервер

```bash
git clone https://github.com/ваш-аккаунт/tg-hosting.git
cd tg-hosting
```

> Или загрузите файлы вручную через SCP / SFTP.

### 4. Настройте переменные окружения

```bash
cp .env.example .env
nano .env
```

Заполните:

```dotenv
BOT_TOKEN=7654321098:AAH...    # Токен от @BotFather
ALLOWED_USERS=123456789        # Ваш Telegram user_id (узнать у @userinfobot)
```

### 5. Создайте рабочие директории

```bash
mkdir -p projects logs
touch hosting.db
```

### 6. Запустите бота через Docker Compose

```bash
docker compose up -d --build
```

Проверьте, что бот запущен:

```bash
docker compose logs -f
```

Вы должны увидеть:
```
tg_hosting_bot  | Инициализация базы данных…
tg_hosting_bot  | Бот запускается (polling)…
```

---

## 🛠 Запуск без Docker (напрямую на сервере)

```bash
# Создать виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# Установить зависимости
pip install -r requirements.txt

# Запустить бота
python bot.py
```

Для фоновой работы используйте **tmux** или **screen**:

```bash
tmux new -s tgbot
python bot.py
# Ctrl+B, D — отключиться от сессии
```

Или создайте systemd-сервис:

```bash
nano /etc/systemd/system/tgbot.service
```

```ini
[Unit]
Description=TG Hosting Bot
After=network.target docker.service

[Service]
User=root
WorkingDirectory=/root/tg-hosting
ExecStart=/root/tg-hosting/venv/bin/python bot.py
Restart=always
RestartSec=5
EnvironmentFile=/root/tg-hosting/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable tgbot
systemctl start tgbot
systemctl status tgbot
```

---

## 🤖 Функции бота

### Главное меню (Reply Keyboard)

| Кнопка | Описание |
|---|---|
| 🚀 Deploy проект | Задеплоить GitHub/GitLab репозиторий |
| 📦 Мои приложения | Список проектов с inline-управлением |
| 📊 Статус сервера | CPU / RAM / Disk / кол-во контейнеров |
| ℹ️ Помощь | Справка и ограничения |

### Inline-меню проекта

| Кнопка | Действие |
|---|---|
| ▶ Запустить | `docker start <container>` |
| ⏹ Остановить | `docker stop <container>` |
| 🔄 Перезапустить | `docker restart <container>` |
| 📜 Логи | Последние 30 строк `docker logs` |
| 🗑 Удалить | Удалить контейнер + файлы + запись в БД |

### Процесс деплоя

```
Пользователь → ссылка на репозиторий
    ↓
git clone --depth=1 <url> projects/<user_id>/<project_id>/
    ↓
pip install -r requirements.txt  (если есть)
    ↓
Ввод env-переменных → .env файл
    ↓
docker run -d \
  --name tghost_<project_id> \
  --memory 512m --cpus 0.5 \
  --restart unless-stopped \
  --env-file .env \
  -v <project_dir>:/app:ro \
  python:3.10-slim python main.py
```

---

## ⚙️ Ограничения

| Параметр | Значение |
|---|---|
| Память на контейнер | 512 MB |
| CPU на контейнер | 0.5 ядра |
| Проектов на пользователя | 3 |
| Поддерживаемые хостинги | GitHub, GitLab |
| Точки входа | main.py, app.py, bot.py, run.py |

---

## 🔒 Безопасность

- **Валидация URL** — принимаются только HTTPS GitHub/GitLab ссылки.
- **Без shell** — все команды запускаются через `subprocess.exec`, исключая shell-инъекции.
- **Изоляция контейнеров** — каждый проект в отдельном Docker-контейнере.
- **Read-only mount** — код монтируется в контейнер только для чтения (`ro`).
- **Белый список** — через `ALLOWED_USERS` можно ограничить доступ к боту.
- **Лимит env** — максимум 50 переменных, 256 символов в строке.

---

## 🔄 Обновление бота

```bash
cd tg-hosting
git pull
docker compose up -d --build
```

---

## 📋 Полезные команды

```bash
# Логи бота
docker compose logs -f

# Список запущенных контейнеров проектов
docker ps --filter "name=tghost_"

# Остановить всё
docker compose down

# Перезапуск бота без пересборки
docker compose restart

# Посмотреть использование ресурсов
docker stats --no-stream
```

---

## 🧹 Очистка

```bash
# Удалить остановленные контейнеры проектов
docker container prune -f --filter "name=tghost_"

# Очистить неиспользуемые образы
docker image prune -f
```

---

## 📦 Зависимости

```
aiogram==3.13.1        # Telegram Bot Framework
aiosqlite==0.20.0      # Async SQLite
psutil==6.1.0          # Системные метрики
python-dotenv==1.0.1   # .env файлы
```

---

## 🆘 Частые проблемы

**Бот не отвечает:**
```bash
docker compose logs --tail=50
# Проверьте BOT_TOKEN в .env
```

**Ошибка "permission denied" при docker run:**
```bash
# Добавьте пользователя в группу docker
usermod -aG docker $USER
# или запускайте от root
```

**git clone не работает:**
- Убедитесь, что репозиторий **публичный**
- Проверьте URL: `https://github.com/user/repo`

**Контейнер сразу останавливается:**
- Убедитесь, что в репозитории есть `main.py`, `app.py`, `bot.py` или `run.py`
- Проверьте логи: кнопка 📜 Логи в боте
