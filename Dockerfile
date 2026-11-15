# Образ для сервера: только runtime-зависимости из uv.lock, без dev-инструментов.
# chroma_db попадает в образ при сборке, база доступа Telegram — в volume /app/data.

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# libgomp1 нужен onnxruntime из chromadb.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=uv /uv /uvx /bin/

# Зависимости отдельным слоем: пересобираются только при изменении lock-файлов.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY agent.py access_store.py main.py telegram_bot.py ./
COPY utils ./utils
# База знаний не хранится в git, но должна лежать рядом при сборке.
COPY chroma_db ./chroma_db

RUN useradd --create-home --uid 10001 --user-group app \
    && mkdir -p /app/data \
    && chown -R app:app /app/data /app/chroma_db

USER app

CMD ["python", "telegram_bot.py"]
