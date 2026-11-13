import asyncio
import logging
import os
import random
import re
import time
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

LOG = logging.getLogger('palama')
DB_PATH = os.getenv('DB_PATH', 'data/monitor.sqlite3')
TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
BASE = 'https://www.avito.ru'
QUERIES = ['Триады Палама']
ITEM_RE = re.compile(r'/(?:[^/?#]+/)*[^/?#]*_(\d{7,})(?:[/?#]|$)')
TRIADS_RE = re.compile(r'триад|священно.?безмолв|исихаст', re.I)
PALAMA_RE = re.compile(r'палам', re.I)
BOOK_RE = re.compile(r'книг|триад|сочинен|творени|трактат|издани|том|священно.?безмолв', re.I)


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute('CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, first_seen TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0)')
    con.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    con.commit()
    return con


def classify(title):
    if TRIADS_RE.search(title) and (PALAMA_RE.search(title) or re.search(r'триад', title, re.I)):
        return 'priority'
    if PALAMA_RE.search(title) and BOOK_RE.search(title):
        return 'normal'
    return None


def extract_items(html):
    soup = BeautifulSoup(html, 'html.parser')
    results = {}
    for a in soup.select('a[href]'):
        href = a.get('href', '')
        match = ITEM_RE.search(href)
        if not match:
            continue
        item_id = match.group(1)
        parent = a.find_parent(attrs={'data-marker': re.compile('item', re.I)})
        title_tag = parent.select_one('[itemprop="name"], [data-marker="item-title"]') if parent else None
        title = (title_tag.get_text(' ', strip=True) if title_tag else a.get('title') or a.get_text(' ', strip=True))
        title = re.sub(r'\s+', ' ', title).strip()[:220]
        level = classify(title)
        if not level:
            continue
        price = ''
        if parent:
            price_tag = parent.select_one('[itemprop="price"], [data-marker="item-price"]')
            if price_tag:
                price = price_tag.get('content') or price_tag.get_text(' ', strip=True)
        url = urljoin(BASE, href.split('?')[0].split('#')[0])
        if urlparse(url).netloc not in ('avito.ru', 'www.avito.ru'):
            continue
        results[item_id] = dict(id=item_id, title=title, url=url, price=price, level=level)
    return list(results.values())


async def telegram(text):
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={
            'chat_id': CHAT_ID, 'text': text, 'disable_web_page_preview': True,
        })
        resp.raise_for_status()
        data = resp.json()
        if not data.get('ok'):
            raise RuntimeError(f'Telegram API error: {data.get("description", "unknown")}')


async def scrape(page, query):
    url = f'{BASE}/rossiya/knigi_i_zhurnaly?q={quote(query)}&s=104'
    response = await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    await page.wait_for_timeout(2500)
    if response is None or response.status >= 400:
        raise RuntimeError(f'HTTP {response.status if response else "no response"}')
    html = await page.content()
    body = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True).lower()[:15000]
    if any(x in body for x in ['доступ ограничен', 'подтвердите, что вы не робот', 'captcha', 'доступ временно ограничен']):
        raise RuntimeError('Авито ограничил доступ или запросил проверку')
    if not any(x in html for x in ('data-marker="item', 'catalog', 'Ничего не найдено', 'ничего не найдено')):
        raise RuntimeError('Не удалось распознать результаты поиска')
    return extract_items(html)


async def run_cycle(browser, con):
    page = await browser.new_page(locale='ru-RU', viewport={'width': 1280, 'height': 900})
    try:
        items = await scrape(page, QUERIES[0])
    finally:
        await page.close()
    LOG.info('Поиск %r: %d подходящих объявлений', QUERIES[0], len(items))
    first_run = con.execute("SELECT value FROM state WHERE key='initialized'").fetchone() is None
    now = datetime.now(timezone.utc).isoformat()
    pending = []
    for item in items:
        row = con.execute('SELECT notified FROM seen WHERE id=?', (item['id'],)).fetchone()
        if row is None:
            con.execute('INSERT INTO seen (id,url,title,first_seen,notified) VALUES (?,?,?,?,?)',
                        (item['id'], item['url'], item['title'], now, 1 if first_run else 0))
            if not first_run:
                pending.append(item)
        elif row[0] == 0 and not first_run:
            pending.append(item)
    if first_run:
        con.execute("INSERT INTO state(key,value) VALUES('initialized',?)", (now,))
    con.commit()
    if first_run:
        await telegram(f'📚 Мониторинг запущен. Сохранено {len(items)} объявлений. Новые объявления будут приходить в Telegram. Запуски — по расписанию GitHub Actions (возможны задержки).')
        return
    pending.sort(key=lambda x: x['level'] != 'priority')
    for item in pending:
        flag = '🔔 ПРИОРИТЕТ: ТРИАДЫ' if item['level'] == 'priority' else '📖 Книга Григория Паламы'
        message = f'{flag}\n\n{item["title"]}\n'
        if item['price']:
            message += f'Цена: {item["price"]}\n'
        message += f'\n{item["url"]}'
        
        try:
            await telegram(message)
            con.execute(
                'UPDATE seen SET notified=1 WHERE id=?',
                (item['id'],)
            )
            con.commit()
        except Exception:
            LOG.exception(
                'Не удалось отправить объявление %s',
                item['id']
            )
            break

    LOG.info(
        'Цикл завершён: %d найдено, %d новых',
        len(items),
        len(pending)
    )


async def main():
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO'),
        format='%(asctime)s %(levelname)s %(message)s'
    )

    con = init_db()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox']
        )

        try:
            while True:
                start = time.monotonic()

                try:
                    await run_cycle(browser, con)
                except Exception:
                    LOG.exception('Ошибка проверки Авито')
                    if os.getenv('RUN_ONCE') == '1':
                        raise

                if os.getenv('RUN_ONCE') == '1':
                    break

                await asyncio.sleep(
                    max(1, INTERVAL - (time.monotonic() - start))
                )
        finally:
            await browser.close()
            con.close()


if __name__ == '__main__':
    asyncio.run(main())

