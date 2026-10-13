import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, MenuButtonCommands
from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import ValidationError

from config import PROJECT_ROOT, Settings
from handlers import admin, donate, menu
from services.ai import Analyst
from services.emojis import EmojiFallback
from services.health import heartbeat
from services.matches import Matches
from services.monitoring import Monitor
from services.notifications import run
from services.users import Users


async def main() -> None:
    try:
        settings = Settings()
        token = settings.telegram_bot_token.get_secret_value()
        validate_token(token)
    except (ValidationError, TokenValidationError):
        raise SystemExit("Проверьте настройки .env: токен, лимиты, LOG_LEVEL и BONUS_OFFERS.") from None

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    users = Users(PROJECT_ROOT / "data" / "users.json")
    matches = Matches(PROJECT_ROOT / "data" / "odds")
    analyst = Analyst(settings, PROJECT_ROOT / "data" / "analysis.json")
    monitor = Monitor(settings, PROJECT_ROOT / "data")
    dispatcher = Dispatcher(users=users, matches=matches, analyst=analyst, settings=settings, monitor=monitor)
    dispatcher.include_routers(admin.router, donate.router, menu.router)

    async with Bot(token=token, default=DefaultBotProperties(parse_mode="HTML")) as bot:
        bot.session.middleware(EmojiFallback(settings.custom_emoji_enabled))
        commands = [
            BotCommand(command="start", description="Выбрать дисциплину"),
            BotCommand(command="info", description="Как работает Betty"),
            BotCommand(command="askbetty", description="Спросить Betty о матче"),
            BotCommand(command="bonus", description="Бонусы букмекеров"),
            BotCommand(command="notifications", description="Подборки и напоминания"),
        ]
        await bot.set_my_commands(
            commands,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        for user_id in settings.admin_user_ids:
            try:
                await bot.set_my_commands(
                    [BotCommand(command="status", description="Состояние бота и источников"), *commands],
                    scope=BotCommandScopeChat(chat_id=user_id),
                )
            except TelegramAPIError as error:
                logging.getLogger(__name__).warning("Меню модератора недоступно (%s)", type(error).__name__)
        notifications = asyncio.create_task(run(bot, users, matches))
        health = asyncio.create_task(heartbeat(PROJECT_ROOT / "data" / "health" / "bot.json"))
        try:
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
        finally:
            for task in (notifications, health):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


if __name__ == "__main__":
    asyncio.run(main())
