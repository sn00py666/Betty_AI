import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonCommands
from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import ValidationError

from config import PROJECT_ROOT, Settings
from handlers import donate, menu
from services.ai import Analyst
from services.matches import Matches
from services.notifications import run
from services.users import Users


async def main() -> None:
    try:
        settings = Settings()
        token = settings.telegram_bot_token.get_secret_value()
        validate_token(token)
    except (ValidationError, TokenValidationError):
        raise SystemExit("Проверьте TELEGRAM_BOT_TOKEN и LOG_LEVEL в .env.") from None

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    users = Users(PROJECT_ROOT / "data" / "users.json")
    matches = Matches(PROJECT_ROOT / "data" / "odds")
    analyst = Analyst(settings, PROJECT_ROOT / "data" / "analysis.json")
    dispatcher = Dispatcher(users=users, matches=matches, analyst=analyst)
    dispatcher.include_routers(donate.router, menu.router)

    async with Bot(token=token, default=DefaultBotProperties(parse_mode="HTML")) as bot:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Выбрать дисциплину"),
                BotCommand(command="info", description="Как работает Betty"),
                BotCommand(command="askbetty", description="Спросить Betty о матче"),
                BotCommand(command="bonus", description="Матчи и коэффициенты"),
                BotCommand(command="notifications", description="Подборки и напоминания"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        notifications = asyncio.create_task(run(bot, users, matches))
        try:
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
        finally:
            notifications.cancel()
            with suppress(asyncio.CancelledError):
                await notifications


if __name__ == "__main__":
    asyncio.run(main())
