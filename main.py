"""Command-line interface for the exam preparation agent."""

import argparse
import asyncio
import sys

from agent import ExamAgent


async def run(question: str) -> None:
    async with ExamAgent.from_env() as agent:
        print(await agent.answer(question))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ответы на вопросы по транслирующим системам"
    )
    parser.add_argument("question", nargs="+", help="Вопрос к зачёту")
    args = parser.parse_args()
    try:
        asyncio.run(run(" ".join(args.question)))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
