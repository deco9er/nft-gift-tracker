import asyncio

from dotenv import load_dotenv

from catalog_bot import ROOT, login


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    asyncio.run(login())
