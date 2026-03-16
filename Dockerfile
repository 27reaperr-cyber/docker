# ── Dockerfile для Telegram-бота хостинга ──────────────────
# Образ: python:3.10-slim
# Устанавливает: git, docker CLI, зависимости Python
# ────────────────────────────────────────────────────────────

FROM python:3.10-slim

# Метаданные
LABEL maintainer="tg-hosting-bot"
LABEL description="Telegram bot for hosting Git projects"

# Не создавать .pyc файлы, логи сразу в stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ── Системные зависимости ────────────────────────────────────
# git       — клонирование репозиториев
# curl      — загрузка docker CLI
# ca-certs  — HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo \
       "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/debian \
       $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python зависимости ───────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Исходный код ─────────────────────────────────────────────
COPY bot.py database.py deploy.py process_manager.py ./

# ── Рабочие директории ───────────────────────────────────────
RUN mkdir -p projects logs

# ── Healthcheck ──────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import aiogram; print('ok')" || exit 1

# ── Запуск ───────────────────────────────────────────────────
CMD ["python", "bot.py"]
