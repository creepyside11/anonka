import asyncio
import logging
import os

import uvicorn

from bot import main as bot_main


async def web_main() -> None:
    config = uvicorn.Config(
        "dashboard:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    bot_task = asyncio.create_task(bot_main(), name="telegram-bot")
    web_task = asyncio.create_task(web_main(), name="web-dashboard")

    done, pending = await asyncio.wait(
        {bot_task, web_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        exc = task.exception()
        if exc is not None:
            raise exc


if __name__ == "__main__":
    asyncio.run(main())
