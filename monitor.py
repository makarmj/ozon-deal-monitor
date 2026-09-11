import asyncio
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

STATE_PATH = Path(".monitor-state.json")
BASE = "https://www.ozon.ru"

# The monitor runs every 5 minutes. PS5 is checked every run; other groups are
# rotated to reduce load on Ozon while still checking the full watchlist often.
RULES = [
    {
        "label": "PS5 Slim Digital",
        "query": "PlayStation 5 Slim Digital CFI-2116B",
        "max_price": 55000,
        "min_price": 30000,
        "include_any": ["playstation 5 slim", "ps5 slim"],
        "include_one_of": ["digital", "цифров", "без дисковод", "cfi-21", "cfi-20"],
        "priority": True,
    },
    {
        "label": "MacBook Air M4",
        "query": "MacBook Air M4 16GB",
        "max_price": 100000,
        "min_price": 45000,
        "include_any": ["macbook air"],
        "include_one_of": ["m4"],
    },
    {
        "label": "MacBook Air M3",
        "query": "MacBook Air M3 16GB",
        "max_price": 90000,
        "min_price": 45000,
        "include_any": ["macbook air"],
        "include_one_of": ["m3"],
    },
    {
        "label": "MacBook Air M2",
        "query": "MacBook Air M2 16GB",
        "max_price": 80000,
        "min_price": 40000,
        "include_any": ["macbook air"],
        "include_one_of": ["m2"],
    },
    {
        "label": "MacBook Pro Apple Silicon",
        "query": "MacBook Pro M4",
        "max_price": 100000,
        "min_price": 55000,
        "include_any": ["macbook pro"],
        "include_one_of": ["m4", "m3 pro", "m3 max", "m2 pro", "m2 max"],
    },
    {
        "label": "RTX 5060 laptop",
        "query": "ноутбук RTX 5060",
        "max_price": 100000,
        "min_price": 45000,
        "include_any": ["rtx 5060", "geforce rtx 5060"],
        "include_one_of": [],
    },
    {
        "label": "RTX 4060 laptop",
        "query": "ноутбук RTX 4060",
        "max_price": 85000,
        "min_price": 40000,
        "include_any": ["rtx 4060", "geforce rtx 4060"],
        "include_one_of": [],
    },
    {
        "label": "ROG Ally Z1 Extreme",
        "query": "ROG Ally Z1 Extreme",
        "max_price": 60000,
        "min_price": 30000,
        "include_any": ["rog ally"],
        "include_one_of": ["z1 extreme", "extreme"],
    },
    {
        "label": "ROG Ally X",
        "query": "ROG Ally X",
        "max_price": 85000,
        "min_price": 40000,
        "include_any": ["rog ally x"],
        "include_one_of": [],
    },
    {
        "label": "Steam Deck OLED 512",
        "query": "Steam Deck OLED 512GB",
        "max_price": 65000,
        "min_price": 30000,
        "include_any": ["steam deck"],
        "include_one_of": ["oled"],
    },
    {
        "label": "Lenovo Legion Go",
        "query": "Lenovo Legion Go",
        "max_price": 75000,
        "min_price": 30000,
        "include_any": ["legion go"],
        "include_one_of": [],
    },
]

PRICE_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[\s\u00a0\u2009]\d{3})+|\d{4,6})\s*₽")
BAD_PRICE_CONTEXT = ("в месяц", "/мес", "×", "балл", "кэшб", "скидк", "эконом")


def load_state():
    if not STATE_PATH.exists():
        return {"alerts": {}, "seen": {}, "run_number": 0}
    try:
        data = json.loads(STATE_PATH.read_text("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("alerts", {})
        data.setdefault("seen", {})
        data.setdefault("run_number", 0)
        return data
    except Exception:
        return {"alerts": {}, "seen": {}, "run_number": 0}


def save_state(state):
    cutoff = int(time.time()) - 7 * 24 * 3600
    state["alerts"] = {
        k: v for k, v in state.get("alerts", {}).items()
        if isinstance(v, int) and v >= cutoff
    }
    seen = {}
    for k, v in state.get("seen", {}).items():
        if isinstance(v, dict) and v.get("ts", 0) >= cutoff:
            seen[k] = v
    state["seen"] = seen
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        "utf-8",
    )


def telegram_chat_id(token):
    explicit = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if explicit:
        return explicit
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
        r.raise_for_status()
        for upd in reversed(r.json().get("result", [])):
            msg = upd.get("message") or upd.get("channel_post")
            cid = (msg or {}).get("chat", {}).get("id")
            if cid is not None:
                return str(cid)
    except Exception as exc:
        print(f"Telegram getUpdates failed: {exc}")
    return ""


def send_telegram(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    r.raise_for_status()


def normalize(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def canonical_product_url(href):
    if href.startswith("/"):
        href = BASE + href
    return href.split("?")[0].split("#")[0]


def extract_prices(text, min_price):
    found = []
    low = text.lower()
    for match in PRICE_RE.finditer(text):
        left = low[max(0, match.start() - 24):match.start()]
        right = low[match.end():min(len(low), match.end() + 24)]
        context = left + " " + right
        if any(bad in context for bad in BAD_PRICE_CONTEXT):
            continue
        try:
            price = int(re.sub(r"\D", "", match.group(1)))
        except ValueError:
            continue
        if min_price <= price <= 300000:
            found.append(price)
    return sorted(set(found))


def rule_matches(rule, text):
    low = text.lower()
    if rule["include_any"] and not any(term in low for term in rule["include_any"]):
        return False
    if rule["include_one_of"] and not any(term in low for term in rule["include_one_of"]):
        return False
    if "macbook" in low and (" intel " in f" {low} " or "восстановлен" in low or "refurb" in low):
        return False
    return True


def alert_message(rule, product, old_price=None):
    price = product["price"]
    title = product["title"]
    url = product["url"]
    delta = ""
    if old_price and old_price > price:
        delta = f"\n📉 Было: {old_price:,} ₽ → стало: <b>{price:,} ₽</b>"
    msg = (
        f"🔥 <b>{html.escape(rule['label'])}</b>\n"
        f"<b>{price:,} ₽</b> — порог: {rule['max_price']:,} ₽"
        f"{delta}\n"
        f"{html.escape(title)}\n\n"
        f'<a href="{html.escape(url)}">Открыть товар на Ozon</a>\n\n'
        f"⚠️ Перед оплатой проверь продавца, состояние, пошлину, регион/ревизию и комплектацию."
    )
    return msg.replace(",", " ")


def active_rules(run_number):
    priority = [r for r in RULES if r.get("priority")]
    normal = [r for r in RULES if not r.get("priority")]
    # 5 rotating rules + priority every run; every normal rule gets checked
    # roughly every 10 minutes over two consecutive runs.
    half = (len(normal) + 1) // 2
    group = normal[:half] if run_number % 2 == 0 else normal[half:]
    return priority + group


async def wait_for_real_page(page):
    # Give the normal browser page time to execute its scripts.
    for _ in range(10):
        await page.wait_for_timeout(1000)
        title = (await page.title()).lower()
        body = (await page.locator("body").inner_text(timeout=5000)).lower()
        blocked = (
            "доступ ограничен" in body
            or "access denied" in body
            or "captcha" in body
            or "проверяем ваш браузер" in body
        )
        if not blocked and ("ozon" in title or "/product/" in (await page.content())):
            return True
    return False


async def fetch_search(page, rule):
    url = f"{BASE}/search/?text={quote(rule['query'])}&from_global=true"
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PlaywrightTimeoutError:
        return [], "navigation timeout"

    status = response.status if response else 0
    print(f"{rule['label']}: browser HTTP {status}")

    if status in (401, 403, 429):
        return [], f"browser HTTP {status}"

    if not await wait_for_real_page(page):
        return [], "Ozon anti-bot/challenge did not clear"

    raw = await page.evaluate(
        """() => {
          const out = [];
          const links = Array.from(document.querySelectorAll('a[href*="/product/"]'));
          const seen = new Set();
          for (const a of links) {
            const href = a.href || a.getAttribute('href') || '';
            if (!href || seen.has(href)) continue;
            seen.add(href);

            let node = a;
            let best = (a.innerText || '').trim();
            for (let i = 0; i < 7 && node; i++, node = node.parentElement) {
              const t = (node.innerText || '').replace(/\\s+/g, ' ').trim();
              if (t.length >= 30 && t.length <= 2500) best = t;
              if (t.includes('₽') && t.length >= 60 && t.length <= 2500) {
                best = t;
                break;
              }
            }
            out.push({
              href,
              title: (a.getAttribute('title') || a.getAttribute('aria-label') || a.innerText || '').trim(),
              text: best
            });
            if (out.length >= 250) break;
          }
          return out;
        }"""
    )

    products = {}
    for item in raw:
        card = normalize(item.get("text", ""))
        if not rule_matches(rule, card):
            continue
        prices = extract_prices(card, rule["min_price"])
        if not prices:
            continue
        price = min(prices)
        if price > rule["max_price"]:
            continue
        product_url = canonical_product_url(item.get("href", ""))
        if "/product/" not in product_url:
            continue
        title = normalize(item.get("title", ""))
        if len(title) < 8:
            title = card[:240]
        products[product_url] = {
            "url": product_url,
            "title": title[:240],
            "price": price,
        }

    return sorted(products.values(), key=lambda x: x["price"])[:10], ""


async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN secret is missing")
    if not chat_id:
        chat_id = telegram_chat_id(token)
    if not chat_id:
        raise SystemExit("Telegram chat not found")

    state = load_state()
    run_number = int(state.get("run_number", 0))
    rules = active_rules(run_number)
    first_run = not bool(state.get("seen"))

    total_alerts = 0
    total_errors = 0
    now = int(time.time())

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7"},
        )
        page = await context.new_page()

        # Warm up the browser session once.
        try:
            await page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
            await wait_for_real_page(page)
        except Exception as exc:
            print(f"Warmup warning: {exc}")

        for idx, rule in enumerate(rules):
            if idx:
                await page.wait_for_timeout(1300)
            try:
                products, error = await fetch_search(page, rule)
                if error:
                    total_errors += 1
                    print(f"{rule['label']} error: {error}")
                    continue

                for product in products:
                    url = product["url"]
                    price = product["price"]
                    old = state["seen"].get(url, {})
                    old_price = old.get("price")
                    state["seen"][url] = {"price": price, "ts": now}

                    # On the first successful scan, build baseline silently.
                    if first_run:
                        continue

                    is_new_good_deal = not old
                    is_price_drop = isinstance(old_price, int) and price < old_price
                    if not (is_new_good_deal or is_price_drop):
                        continue

                    fingerprint = hashlib.sha1(f"{url}|{price}".encode()).hexdigest()
                    if fingerprint in state["alerts"]:
                        continue

                    send_telegram(token, chat_id, alert_message(rule, product, old_price))
                    state["alerts"][fingerprint] = now
                    total_alerts += 1
                    print(f"Alert sent: {rule['label']} {price} {url}")

            except Exception as exc:
                total_errors += 1
                print(f"{rule['label']} exception: {type(exc).__name__}: {exc}")

        await browser.close()

    state["run_number"] = run_number + 1
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_run_alerts"] = total_alerts
    state["last_run_errors"] = total_errors
    save_state(state)
    print(
        f"Finished: {total_alerts} alerts, {total_errors} rule errors, "
        f"first_run={first_run}, checked_rules={len(rules)}"
    )

    # Make anti-bot failure visible as a failed workflow rather than a false green check.
    if total_errors == len(rules):
        raise SystemExit("All Ozon browser checks failed; likely IP/anti-bot blocking.")


if __name__ == "__main__":
    asyncio.run(main())
