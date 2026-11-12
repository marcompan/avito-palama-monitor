import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

LOG = logging.getLogger('palama')
DB_PATH = os.getenv('DB_PATH', 'data/monitor.sqlite3')
INTERVAL = max(300, int(os.getenv('CHECK_INTERVAL_SECONDS', '600')))
TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
BASE = 'https://www.avito.ru'
QUERIES = [
    'Григорий Палама',
    'Триады Палама',
    'Триады в защиту священно-безмолвствующих',
    'святитель Григорий Палама книга',
]
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


def classify(title, query):
    # Search snippets may contain unrelated words; require a relevant title.
    if TRIADS_RE.search(title) and (PALAMA_RE.search(title) or 'триад' in title.lower()):
        return 'priority'
    if PALAMA_RE.search(title) and BOOK_RE.search(title):
        return 'normal'
    if PALAMA_RE.search(title) and 'палама' in query.lower():
        return 'normal'
    return None


def extract_items(html, query):
    soup = BeautifulSoup(html, 'html.parser')
    results = {}
    for a in soup.select('a[href]'):
        href = a.get('href', '')
        match = ITEM_RE.search(href)
        if not match:
            continue
        item_id = match.group(1)
        title = a.get('title') or a.get_text(' ', strip=True)
        if not title or len(title) < 5:
            parent = a.find_parent(attrs={'data-marker': re.compile('item', re.I)})
            if parent:
                title_tag = parent.select_one('[itemprop="name"], [data-marker="item-title"]')
                if title_tag:
                    title = title_tag.get_text(' ', strip=True)
        title = re.sub(r'\s+', ' ', title).strip()[:220]
        level = classify(title, query)
        if not level:
            continue
        parent = a.find_parent(attrs={'data-marker': re.compile('item', re.I)})
        price = ''
        if parent:
            price_tag = parent.select_one('[itemprop="price"], [data-marker="item-price"]')
            if price_tag:
                price = price_tag.get('content') or price_tag.get_text(' ', strip=True)
        url = urljoin(BASE, href.split('?')[0].split('#')[0])
        if urlparse(url).netloc not in ('avito.ru', 'www.avito.ru'):
            continue
        old = results.get(item_id)
        if old is None or (level == 'priority' and old['level'] != 'priority'):
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
    html = await page.content()
    body = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True).lower()[:15000]
    if response is None or response.status >= 400:
        raise RuntimeError(f'HTTP {response.status if response else "no response"}')
    if any(x in body for x in ['доступ ограничен', 'подтвердите, что вы не робот', 'captcha', 'доступ временно ограничен']):
        raise RuntimeError('Avito ограничил доступ или запросил проверку')
    # Do not mark a query as successful if the page lacks recognizable search structure.
    if not any(x in html for x in ('data-marker="item', 'data-marker=\\"item', 'catalog', 'Ничего не найдено', 'ничего не найдено')):
        raise RuntimeError('Неизвестная структура страницы Авито; проверьте селекторы')
    return extract_items(html, query)


async def run_cycle(browser, con):
    page = await browser.new_page(locale='ru-RU', viewport={'width': 1280, 'height': 900})
    all_items = {}
    successful = 0
    try:
        for query in QUERIES:
            try:
                items = await scrape(page, query)
                successful += 1
                for item in items:
                    prev = all_items.get(item['id'])
                    if not prev or item['level'] == 'priority':
                        all_items[item['id']] = item
                LOG.info('Запрос %r: %s совпадений', query, len(items))
            except Exception as exc:
                LOG.warning('Ошибка запроса %r: %s', query, exc)
            await asyncio.sleep(random.uniform(2, 5))
    finally:
        await page.close()
    if not successful:
        raise RuntimeError('Ни один запрос к Авито не завершился успешно')

    first_run = con.execute("SELECT value FROM state WHERE key='initialized'").fetchone() is None
    now = datetime.now(timezone.utc).isoformat()
    new_items = []
    for item in all_items.values():
        row = con.execute('SELECT notified FROM seen WHERE id=?', (item['id'],)).fetchone()
        if row is None:
            con.execute('INSERT INTO seen (id, url, title, first_seen, notified) VALUES (?,?,?,?,?)',
                        (item['id'], item['url'], item['title'], now, 1 if first_run else 0))
            if not first_run:
                new_items.append(item)
        elif row[0] == 0 and not first_run:
            new_items.append(item)
    if first_run:
        con.execute("INSERT INTO state(key,value) VALUES('initialized',?)", (now,))
    con.commit()
    if first_run:
        LOG.info('Первый запуск: сохранено %d объявлений без уведомлений', len(all_items))
        await telegram(f'📚 Мониторинг книг Григория Паламы запущен. Первичная база: {len(all_items)} объявлений. Проверка каждые {INTERVAL // 60} мин.')
        return
    new_items.sort(key=lambda x: x['level'] != 'priority')
    for item in new_items:
        flag = '🔔 ПРИОРИТЕТ: ТРИАДЫ' if item['level'] == 'priority' else '📖 Книга Григория Паламы'
        message = f'{flag}\n\n{item["title"]}\n'
        if item['price']:
            message += f'Цена: {item["price"]}\n'
        message += f'\n{item["url"]}'
        try:
            await telegram(message)
            con.execute('UPDATE seen SET notified=1 WHERE id=?', (item['id'],))
            con.commit()
        except Exception:
            LOG.exception('Не удалось отправить объявление %s; повторим позже', item['id'])
            break
    LOG.info('Цикл завершён: %d найдено, %d новых', len(all_items), len(new_items))


async def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
    con = init_db()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            while True:
                start = time.monotonic()
                try:
                    await run_cycle(browser, con)
                except Exception:
                    LOG.exception('Цикл завершился ошибкой; следующая попытка по расписанию')
                    if os.getenv('RUN_ONCE') == '1':
                        raise
                if os.getenv('RUN_ONCE') == '1':
                    break
                await asyncio.sleep(max(1, INTERVAL - (time.monotonic() - start)))
        finally:
            await browser.close()
            con.close()


if __name__ == '__main__':
    asyncio.run(main())
