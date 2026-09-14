import hashlib
import html
import json
import os
import sys
import time
from pathlib import Path

import requests

ACTOR_ID = "isolovyev~ru-marketplaces-price-monitor"
API_BASE = "https://api.apify.com/v2"
STATE_PATH = Path(".monitor-state.json")

POLL_INTERVAL_SECONDS = 10
MAX_WAIT_SECONDS = 12 * 60
MAX_ALERTS_PER_RUN = 12

RULES = [
    {"label":"PS5 Slim Digital","query":"PlayStation 5 Slim Digital","max_price":55000,"min_price":30000,
     "include_any":["playstation 5","ps5"],"include_one_of":["digital","цифров","без дисковод"],"exclude":["disc drive","дисковод отдельно"]},
    {"label":"MacBook Air M4","query":"MacBook Air M4 16GB","max_price":100000,"min_price":45000,
     "include_any":["macbook air"],"include_one_of":["m4"],"exclude":["intel","восстановлен","refurb"]},
    {"label":"MacBook Air M3","query":"MacBook Air M3 16GB","max_price":90000,"min_price":45000,
     "include_any":["macbook air"],"include_one_of":["m3"],"exclude":["intel","восстановлен","refurb"]},
    {"label":"MacBook Air M2","query":"MacBook Air M2 16GB","max_price":80000,"min_price":40000,
     "include_any":["macbook air"],"include_one_of":["m2"],"exclude":["intel","восстановлен","refurb"]},
    {"label":"MacBook Pro Apple Silicon","query":"MacBook Pro M4 16GB","max_price":100000,"min_price":55000,
     "include_any":["macbook pro"],"include_one_of":["m4","m3","m2"],"exclude":["intel","восстановлен","refurb"]},
    {"label":"RTX 5060 laptop","query":"ноутбук RTX 5060","max_price":100000,"min_price":45000,
     "include_any":["rtx 5060","geforce rtx 5060"],"include_one_of":[],"exclude":["видеокарта отдельно"]},
    {"label":"RTX 4060 laptop","query":"ноутбук RTX 4060","max_price":85000,"min_price":40000,
     "include_any":["rtx 4060","geforce rtx 4060"],"include_one_of":[],"exclude":["видеокарта отдельно"]},
    {"label":"ROG Ally Z1 Extreme","query":"ROG Ally Z1 Extreme","max_price":60000,"min_price":30000,
     "include_any":["rog ally"],"include_one_of":["z1 extreme","extreme"],"exclude":[]},
    {"label":"ROG Ally X","query":"ROG Ally X","max_price":85000,"min_price":40000,
     "include_any":["rog ally x"],"include_one_of":[],"exclude":[]},
    {"label":"Steam Deck OLED 512","query":"Steam Deck OLED 512GB","max_price":65000,"min_price":30000,
     "include_any":["steam deck"],"include_one_of":["oled"],"exclude":[]},
    {"label":"Lenovo Legion Go","query":"Lenovo Legion Go","max_price":75000,"min_price":30000,
     "include_any":["legion go"],"include_one_of":[],"exclude":[]},
]
RULE_BY_QUERY = {r["query"].lower(): r for r in RULES}

TERMINAL_SUCCESS = {"SUCCEEDED"}
TERMINAL_FAILURE = {"FAILED", "TIMED-OUT", "ABORTED"}

def load_state():
    if not STATE_PATH.exists():
        return {"alerts": {}, "apify_initialized": False}
    try:
        state = json.loads(STATE_PATH.read_text("utf-8"))
        if not isinstance(state, dict):
            raise ValueError
    except Exception:
        state = {}
    state.setdefault("alerts", {})
    state.setdefault("apify_initialized", False)
    return state

def save_state(state):
    cutoff = int(time.time()) - 14 * 24 * 3600
    state["alerts"] = {
        k: v for k, v in state.get("alerts", {}).items()
        if isinstance(v, int) and v >= cutoff
    }
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        "utf-8",
    )

def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

def send_telegram(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    r.raise_for_status()

def normalize(v):
    return " ".join(str(v or "").split())

def title_matches(rule, title):
    low = title.lower()
    if rule["include_any"] and not any(x in low for x in rule["include_any"]):
        return False
    if rule["include_one_of"] and not any(x in low for x in rule["include_one_of"]):
        return False
    if any(x in low for x in rule.get("exclude", [])):
        return False
    return True

def as_price(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None

def actor_input():
    return {
        "mode": "monitor",
        "platforms": ["ozon"],
        "queries": [r["query"] for r in RULES],
        "maxPagesPerQuery": 1,
        "maxItemsPerQuery": 15,
        "alertOnly": True,
        "flagUnderpriced": True,
        "proxyConfiguration": {
            "useApifyProxy": True,
            "apifyProxyGroups": ["RESIDENTIAL"],
            "apifyProxyCountry": "RU",
        },
    }

def start_actor(token):
    url = f"{API_BASE}/actors/{ACTOR_ID}/runs"
    r = requests.post(
        url,
        headers=auth_headers(token),
        json=actor_input(),
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Apify start HTTP {r.status_code}: {r.text[:1200]}")
    payload = r.json()
    data = payload.get("data", payload)
    run_id = data.get("id")
    if not run_id:
        raise RuntimeError(f"Apify did not return run id: {str(payload)[:1000]}")
    print(f"Apify run started: {run_id}")
    return run_id

def get_run(token, run_id):
    r = requests.get(
        f"{API_BASE}/actor-runs/{run_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Apify status HTTP {r.status_code}: {r.text[:1000]}")
    payload = r.json()
    return payload.get("data", payload)

def wait_for_actor(token, run_id):
    started = time.time()
    last_status = None

    while True:
        run = get_run(token, run_id)
        status = str(run.get("status", "")).upper()

        if status != last_status:
            print(f"Apify status: {status}")
            last_status = status

        if status in TERMINAL_SUCCESS:
            return run

        if status in TERMINAL_FAILURE:
            msg = run.get("statusMessage") or run.get("status_message") or ""
            raise RuntimeError(f"Apify run ended with {status}: {msg}")

        elapsed = time.time() - started
        if elapsed >= MAX_WAIT_SECONDS:
            raise RuntimeError(
                f"Apify run {run_id} still not finished after {MAX_WAIT_SECONDS} seconds"
            )

        time.sleep(POLL_INTERVAL_SECONDS)

def fetch_dataset_items(token, run_id):
    r = requests.get(
        f"{API_BASE}/actor-runs/{run_id}/dataset/items",
        headers={"Authorization": f"Bearer {token}"},
        params={"clean": "true", "format": "json"},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Apify dataset HTTP {r.status_code}: {r.text[:1200]}")
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected dataset response: {str(data)[:700]}")
    return data

def find_rule(item):
    q = normalize(item.get("query")).lower()
    if q in RULE_BY_QUERY:
        return RULE_BY_QUERY[q]
    title = normalize(item.get("title"))
    matches = [r for r in RULES if title_matches(r, title)]
    return matches[0] if len(matches) == 1 else None

def format_alert(rule, item):
    title = normalize(item.get("title"))
    url = normalize(item.get("url"))
    price = as_price(item.get("price"))
    prev = as_price(item.get("previousPrice"))
    change = normalize(item.get("changeType")).lower()

    lines = []
    if prev and prev > price:
        lines.append(f"📉 Было: {prev:,} ₽ → стало: <b>{price:,} ₽</b>")
    elif change == "new":
        lines.append("🆕 Новая карточка в выдаче")

    if item.get("isUnderpriced"):
        lines.append("🚩 Actor отметил цену как аномально низкую")

    extra = ("\n".join(lines) + "\n") if lines else ""

    text = (
        f"🔥 <b>{html.escape(rule['label'])}</b>\n"
        f"<b>{price:,} ₽</b> — наш порог: {rule['max_price']:,} ₽\n"
        f"{extra}{html.escape(title)}\n\n"
        f'<a href="{html.escape(url)}">Открыть товар на Ozon</a>\n\n'
        "⚠️ Перед оплатой проверь продавца, состояние, регион/ревизию, "
        "комплектацию, цену по Ozon Карте и возможную пошлину."
    )
    return text.replace(",", " ")

def process_items(items, state, tg_token, chat_id):
    candidates = []

    for item in items:
        platform = normalize(item.get("platform")).lower()
        if platform not in ("", "ozon"):
            continue

        rule = find_rule(item)
        if not rule:
            print(
                "Skip unmapped:",
                normalize(item.get("query")),
                normalize(item.get("title"))[:120],
            )
            continue

        title = normalize(item.get("title"))
        price = as_price(item.get("price"))
        change = normalize(item.get("changeType")).lower()

        if not title or price is None:
            continue
        if not title_matches(rule, title):
            continue
        if not (rule["min_price"] <= price <= rule["max_price"]):
            continue
        if change and change not in ("new", "price_down"):
            continue

        candidates.append((price, rule, item))

    candidates.sort(key=lambda x: x[0])

    first = not bool(state.get("apify_initialized"))
    sent = 0
    now = int(time.time())

    # First successful run is a silent baseline to avoid a notification storm.
    if first:
        print(f"Baseline run: {len(candidates)} matching candidate(s), no deal alerts sent.")
    else:
        for price, rule, item in candidates:
            if sent >= MAX_ALERTS_PER_RUN:
                print("Alert cap reached.")
                break

            url = normalize(item.get("url"))
            identity = url or f"{rule['label']}|{normalize(item.get('title'))}"
            fp = hashlib.sha1(f"{identity}|{price}".encode("utf-8")).hexdigest()

            if fp in state["alerts"]:
                continue

            send_telegram(tg_token, chat_id, format_alert(rule, item))
            state["alerts"][fp] = now
            sent += 1
            print(f"Alert sent: {rule['label']} — {price} ₽ — {url}")

    if first:
        send_telegram(
            tg_token,
            chat_id,
            "✅ <b>Ozon-монитор через Apify запущен.</b>\n"
            f"Первый успешный проход завершён. Actor вернул {len(items)} "
            f"новых/изменённых карточек. Это базовый проход — без спама сделками.\n\n"
            "Следующие изменения и новые выгодные карточки будут присылаться сюда.",
        )

    state["apify_initialized"] = True
    state["last_apify_run_unix"] = now
    state["last_apify_items"] = len(items)
    state["last_alerts_sent"] = sent

    return sent

def main():
    apify = os.getenv("APIFY_TOKEN", "").strip()
    tg = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    missing = [
        name for name, value in [
            ("APIFY_TOKEN", apify),
            ("TELEGRAM_BOT_TOKEN", tg),
            ("TELEGRAM_CHAT_ID", chat),
        ] if not value
    ]
    if missing:
        raise SystemExit("Missing GitHub Actions secret(s): " + ", ".join(missing))

    state = load_state()

    print("Starting Apify asynchronously...")
    run_id = start_actor(apify)
    wait_for_actor(apify, run_id)

    items = fetch_dataset_items(apify, run_id)
    print(f"Apify returned {len(items)} dataset item(s).")

    sent = process_items(items, state, tg, chat)
    save_state(state)

    print(f"Finished: {sent} Telegram alert(s), {len(items)} Apify item(s).")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
