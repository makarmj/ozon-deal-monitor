import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

STATE_PATH = Path('.monitor-state.json')
BASE = 'https://www.ozon.ru'

RULES = [
    {
        'label': 'PS5 Slim Digital',
        'query': 'PlayStation 5 Slim Digital',
        'max_price': 55000,
        'min_price': 30000,
        'include_any': ['playstation 5 slim', 'ps5 slim'],
        'include_one_of': ['digital', 'цифров', 'без дисковод'],
    },
    {
        'label': 'MacBook Air M4',
        'query': 'MacBook Air M4 16GB',
        'max_price': 100000,
        'min_price': 45000,
        'include_any': ['macbook air'],
        'include_one_of': ['m4'],
    },
    {
        'label': 'MacBook Air M3',
        'query': 'MacBook Air M3 16GB',
        'max_price': 90000,
        'min_price': 45000,
        'include_any': ['macbook air'],
        'include_one_of': ['m3'],
    },
    {
        'label': 'MacBook Air M2',
        'query': 'MacBook Air M2 16GB',
        'max_price': 80000,
        'min_price': 40000,
        'include_any': ['macbook air'],
        'include_one_of': ['m2'],
    },
    {
        'label': 'MacBook Pro Apple Silicon',
        'query': 'MacBook Pro M4',
        'max_price': 100000,
        'min_price': 55000,
        'include_any': ['macbook pro'],
        'include_one_of': ['m4', 'm3 pro', 'm3 max', 'm2 pro', 'm2 max'],
    },
    {
        'label': 'RTX 5060 laptop',
        'query': 'ноутбук RTX 5060',
        'max_price': 100000,
        'min_price': 45000,
        'include_any': ['rtx 5060', 'geforce rtx 5060'],
        'include_one_of': [],
    },
    {
        'label': 'RTX 4060 laptop',
        'query': 'ноутбук RTX 4060',
        'max_price': 85000,
        'min_price': 40000,
        'include_any': ['rtx 4060', 'geforce rtx 4060'],
        'include_one_of': [],
    },
    {
        'label': 'ROG Ally Z1 Extreme',
        'query': 'ROG Ally Z1 Extreme',
        'max_price': 60000,
        'min_price': 30000,
        'include_any': ['rog ally'],
        'include_one_of': ['z1 extreme', 'extreme'],
    },
    {
        'label': 'ROG Ally X',
        'query': 'ROG Ally X',
        'max_price': 85000,
        'min_price': 40000,
        'include_any': ['rog ally x'],
        'include_one_of': [],
    },
    {
        'label': 'Steam Deck OLED 512',
        'query': 'Steam Deck OLED 512GB',
        'max_price': 65000,
        'min_price': 30000,
        'include_any': ['steam deck'],
        'include_one_of': ['oled'],
    },
    {
        'label': 'Lenovo Legion Go',
        'query': 'Lenovo Legion Go',
        'max_price': 75000,
        'min_price': 30000,
        'include_any': ['legion go'],
        'include_one_of': [],
    },
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
}

PRICE_RE = re.compile(r'(?<!\d)(\d{1,3}(?:[\s\u00a0\u2009]\d{3})+|\d{4,6})\s*₽')
BAD_PRICE_CONTEXT = ('в месяц', '/мес', 'балл', 'кэшб', 'эконом')


def load_state():
    if not STATE_PATH.exists():
        return {'initialized': False, 'alerts': {}}
    try:
        data = json.loads(STATE_PATH.read_text('utf-8'))
        if not isinstance(data, dict):
            return {'initialized': False, 'alerts': {}}
        data.setdefault('initialized', False)
        data.setdefault('alerts', {})
        return data
    except Exception:
        return {'initialized': False, 'alerts': {}}


def save_state(state):
    cutoff = int(time.time()) - 14 * 24 * 3600
    alerts = state.get('alerts', {})
    state['alerts'] = {
        k: v for k, v in alerts.items()
        if isinstance(v, dict) and int(v.get('seen_at', 0)) >= cutoff
    }
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), 'utf-8')


def telegram_chat_id(token):
    explicit = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if explicit:
        return explicit
    try:
        r = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=15)
        r.raise_for_status()
        updates = r.json().get('result', [])
        for upd in reversed(updates):
            msg = upd.get('message') or upd.get('channel_post')
            chat_id = (msg or {}).get('chat', {}).get('id')
            if chat_id is not None:
                return str(chat_id)
    except Exception as exc:
        print(f'Telegram getUpdates failed: {exc}')
    return ''


def send_telegram(token, chat_id, text):
    r = requests.post(
        f'https://api.telegram.org/bot{token}/sendMessage',
        json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False,
        },
        timeout=20,
    )
    r.raise_for_status()


def normalize(s):
    return re.sub(r'\s+', ' ', (s or '')).strip()


def canonical_product_url(href):
    full = urljoin(BASE, href)
    return full.split('?')[0].split('#')[0]


def surrounding_card_text(anchor):
    best = normalize(anchor.get_text(' ', strip=True))
    node = anchor
    for _ in range(7):
        node = getattr(node, 'parent', None)
        if node is None:
            break
        text = normalize(node.get_text(' ', strip=True))
        if 20 <= len(text) <= 2200:
            best = text
        if '₽' in text and len(text) >= 60:
            return text
    return best


def extract_prices(text, min_price):
    found = []
    low = text.lower()
    for match in PRICE_RE.finditer(text):
        left = low[max(0, match.start() - 24): match.start()]
        right = low[match.end(): min(len(low), match.end() + 24)]
        context = left + ' ' + right
        if any(bad in context for bad in BAD_PRICE_CONTEXT):
            continue
        try:
            price = int(re.sub(r'\D', '', match.group(1)))
        except ValueError:
            continue
        if min_price <= price <= 300000:
            found.append(price)
    return sorted(set(found))


def rule_matches(rule, text):
    low = text.lower()
    if rule['include_any'] and not any(term in low for term in rule['include_any']):
        return False
    if rule['include_one_of'] and not any(term in low for term in rule['include_one_of']):
        return False
    if 'macbook' in low and (' intel ' in f' {low} ' or 'восстановлен' in low or 'refurb' in low):
        return False
    return True


def fetch_search(session, rule):
    url = f"{BASE}/search/?text={quote(rule['query'])}&from_global=true"
    response = session.get(url, timeout=25)
    print(f"{rule['label']}: HTTP {response.status_code}, {len(response.text)} bytes")
    if response.status_code != 200:
        return [], f'HTTP {response.status_code}'
    body_low = response.text.lower()
    if len(response.text) < 7000 or 'captcha' in body_low or 'доступ ограничен' in body_low:
        return [], 'Ozon anti-bot/CAPTCHA or empty response'

    soup = BeautifulSoup(response.text, 'html.parser')
    products = {}
    for anchor in soup.select('a[href*="/product/"]'):
        href = anchor.get('href') or ''
        product_url = canonical_product_url(href)
        if product_url in products:
            continue
        card = surrounding_card_text(anchor)
        if not rule_matches(rule, card):
            continue
        prices = extract_prices(card, rule['min_price'])
        if not prices:
            continue
        price = min(prices)
        if price > rule['max_price']:
            continue

        title = normalize(anchor.get('title') or anchor.get('aria-label') or anchor.get_text(' ', strip=True))
        if len(title) < 8:
            title = card[:240]
        products[product_url] = {
            'url': product_url,
            'title': title[:240],
            'price': price,
            'card': card[:700],
        }

    return sorted(products.values(), key=lambda x: x['price'])[:8], ''


def fingerprint(product):
    return hashlib.sha1(f"{product['url']}|{product['price']}".encode()).hexdigest()


def alert_message(rule, product):
    price = product['price']
    title = product['title']
    url = product['url']
    return (
        f"🔥 <b>{html.escape(rule['label'])}</b>\n"
        f"<b>{price:,} ₽</b> — наш порог: {rule['max_price']:,} ₽\n"
        f"{html.escape(title)}\n\n"
        f"<a href=\"{html.escape(url)}\">Открыть товар на Ozon</a>\n\n"
        f"⚠️ Перед оплатой проверь продавца, состояние товара, пошлину, регион/ревизию и комплектацию."
    ).replace(',', ' ')


def main():
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('TELEGRAM_BOT_TOKEN secret is missing')
    chat_id = telegram_chat_id(token)
    if not chat_id:
        raise SystemExit('Telegram chat not found. Open the bot in Telegram and send /start, then run again.')

    state = load_state()
    first_run = not state.get('initialized', False)
    session = requests.Session()
    session.headers.update(HEADERS)

    total_alerts = 0
    total_errors = 0
    now = int(time.time())

    for idx, rule in enumerate(RULES):
        if idx:
            time.sleep(1.2)
        try:
            products, error = fetch_search(session, rule)
            if error:
                total_errors += 1
                print(f"{rule['label']} error: {error}")
                continue
            for product in products:
                key = fingerprint(product)
                if key in state['alerts']:
                    continue
                if not first_run:
                    send_telegram(token, chat_id, alert_message(rule, product))
                    total_alerts += 1
                    print(f"Alert sent: {rule['label']} {product['price']} {product['url']}")
                state['alerts'][key] = {
                    'seen_at': now,
                    'label': rule['label'],
                    'price': product['price'],
                    'url': product['url'],
                }
        except Exception as exc:
            total_errors += 1
            print(f"{rule['label']} exception: {exc}")

    state['initialized'] = True
    state['last_run_utc'] = datetime.now(timezone.utc).isoformat()
    state['last_run_alerts'] = total_alerts
    state['last_run_errors'] = total_errors
    save_state(state)
    print(f'Finished: {total_alerts} alerts, {total_errors} rule errors, first_run={first_run}')


if __name__ == '__main__':
    main()
