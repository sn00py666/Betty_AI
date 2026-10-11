"""Отдельный процесс: отправляет алерты даже при остановке основного бота."""

import asyncio
import logging

from aiogram import Bot

from config import PROJECT_ROOT, Settings
from services.monitoring import Monitor


async def main():
    settings = Settings()
    logging.basicConfig(level=settings.log_level)
    monitor = Monitor(settings, PROJECT_ROOT / "data")
    async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
        while True:
            try:
                await monitor.check(bot)
            except (OSError, ValueError, TypeError, KeyError) as error:
                logging.getLogger(__name__).error("Ошибка мониторинга (%s)", type(error).__name__)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
