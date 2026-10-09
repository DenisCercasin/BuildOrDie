import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from bookpool_bot.backend import HttpBackendClient, MockBackendClient
from bookpool_bot.config import Settings
from bookpool_bot.handlers import commands, fallback, groups, requests
from bookpool_bot.merchant_search import MerchantSearchClient
from bookpool_bot.notifications import NotificationDispatcher
from bookpool_bot.parser import BookRequestParser
from bookpool_bot.team_backend import TeamBackendClient


async def run() -> None:
    settings = Settings()
    if not settings.telegram_bot_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env before launching the bot.")
    if settings.backend_mode in {"http", "team"} and not settings.backend_api_key:
        raise SystemExit("Set BACKEND_API_KEY when using an HTTP backend.")
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    backend = (
        MockBackendClient()
        if settings.backend_mode == "mock"
        else TeamBackendClient(
            settings.backend_base_url, settings.backend_api_key, settings.team_backend_state_file
        )
        if settings.backend_mode == "team"
        else HttpBackendClient(settings.backend_base_url, settings.backend_api_key)
    )
    parser = BookRequestParser(
        settings.llm_provider,
        settings.llm_model,
        settings.ollama_base_url,
        settings.llm_base_url,
        settings.llm_api_key,
    )
    merchant_search = MerchantSearchClient() if settings.backend_mode == "team" else None
    bot = Bot(settings.telegram_bot_token)
    dp = Dispatcher(
        storage=MemoryStorage(), backend=backend, parser=parser, merchant_search=merchant_search
    )
    dp.include_routers(commands, requests, groups, fallback)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        logging.getLogger(__name__).exception("update_failed", exc_info=event.exception)
        message = event.update.message or (
            event.update.callback_query.message if event.update.callback_query else None
        )
        if message:
            await message.answer("BookPool hit a temporary problem. Please try again in a moment.")
        if event.update.callback_query:
            await event.update.callback_query.answer()
        return True

    dispatcher = NotificationDispatcher(bot, backend, settings.event_poll_seconds)
    notifier = asyncio.create_task(dispatcher.run())
    try:
        await dp.start_polling(bot)
    finally:
        notifier.cancel()
        await asyncio.gather(notifier, return_exceptions=True)
        if isinstance(backend, (HttpBackendClient, TeamBackendClient)):
            await backend.close()
        if merchant_search is not None:
            await merchant_search.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
