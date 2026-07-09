# test_crawler.py
import asyncio
import logging
from app.crawler import crawl_meta

logging.basicConfig(level=logging.INFO)

async def main():
    result = await crawl_meta("https://www.instagram.com/p/DZvsxw0FdNN/?igsh=eW45dnBuN3lya3p1")
    print(result.model_dump())

asyncio.run(main())