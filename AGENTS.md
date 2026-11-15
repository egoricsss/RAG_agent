# AGENTS.md

RAG-ассистент по курсу «Транслирующие системы»: агент на pydantic-ai поверх
ChromaDB и закрытый Telegram-бот.

## Карта документации

| Документ | Когда читать |
|---|---|
| [docs/agent.md](docs/agent.md) | правки `agent.py`, `main.py`: поиск, промпт, модели, уверенность |
| [docs/telegram-bot.md](docs/telegram-bot.md) | правки `telegram_bot.py`, `access_store.py`: доступ, хендлеры, фото |
| [docs/knowledge-base.md](docs/knowledge-base.md) | ChromaDB, чанкинг, `utils/pdf_embeding_converter.py` |
| [README.md](README.md) | пользовательский запуск и поведение для людей |

Начинай с этого файла, подробности бери в нужном документе. Не дублируй сюда
детали — они живут на уровень ниже.

## Стек

Python 3.14, uv, pydantic-ai + OpenRouter, ChromaDB (cosine), python-telegram-bot 22.

## Команды

| Команда | Что делает |
|---|---|
| `uv run main.py "вопрос"` | ответ в CLI |
| `uv run telegram_bot.py` | Telegram-бот (long polling) |
| `uv run python -m unittest discover -s tests -v` | тесты |
| `uv run ruff check .` | линт |

## Правила

- Python запускай только через `uv`, не через `python`/`python3`/`pip`.
- После изменений прогоняй тесты: они не ходят в сеть и не требуют ключей.
- Не коммить секреты: `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `telegram_access.sqlite3`.
- `chroma_db/` — готовые данные: не пересоздавай и не чисти без явной просьбы.
- Уверенность ответа и косинусное сходство — не вероятность правильности;
  не подавай их пользователю как вероятность.
