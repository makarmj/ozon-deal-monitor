import hashlib
import html
import json
import os
import sys
import time
from pathlib import Path

import requests

ACTOR_ID = "isolovyev~ru-marketplaces-price-monitor"
APIFY_SYNC_URL = f"https://api.apify.com/v2/actors/{ACTOR_ID}/run-sync-get-dataset-items"
STATE_PATH = Path(".monitor-state.json")
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

def load_state():
    if not STATE_PATH.exists():
        return {"alerts": {}, "apify_initialized": False}
    try:
        s = json.loads(STATE_PATH.read_text("utf-8"))
        if not isinstance(s, dict): raise ValueError
    except Exception:
        s = {}
    s.setdefault("alerts", {})
    s.setdefault("apify_initialized", False)
    return s

def save_state(state):
    cutoff = int(time.time()) - 14*24*3600
    state["alerts"] = {k:v for k,v in state.get("alerts",{}).items() if isinstance(v,int) and v >= cutoff}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), "utf-8")

def send_telegram(token, chat_id, text):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":False},
                      timeout=30)
    r.raise_for_status()

def normalize(v): return " ".join(str(v or "").split())

def title_matches(rule, title):
    low = title.lower()
    if rule["include_any"] and not any(x in low for x in rule["include_any"]): return False
    if rule["include_one_of"] and not any(x in low for x in rule["include_one_of"]): return False
    if any(x in low for x in rule.get("exclude", [])): return False
    return True

def as_price(v):
    try: return int(round(float(v)))
    except (TypeError, ValueError): return None

def build_actor_input():
    return {
        "mode":"monitor",
        "platforms":["ozon"],
        "queries":[r["query"] for r in RULES],
        "maxPagesPerQuery":1,
        "maxItemsPerQuery":15,
        "alertOnly":True,
        "flagUnderpriced":True,
        "proxyConfiguration":{
            "useApifyProxy":True,
            "apifyProxyGroups":["RESIDENTIAL"],
            "apifyProxyCountry":"RU"
        }
    }

def run_apify(token):
    params={"token":token,"clean":"true","format":"json"}
    try:
        r=requests.post(APIFY_SYNC_URL, params=params, json=build_actor_input(), timeout=295)
    except requests.Timeout:
        raise RuntimeError("Apify Actor timed out")
    if r.status_code != 200:
        raise RuntimeError(f"Apify HTTP {r.status_code}: {r.text[:1200]}")
    data=r.json()
    if not isinstance(data,list):
        raise RuntimeError(f"Unexpected Apify response: {str(data)[:500]}")
    return data

def find_rule(item):
    q=normalize(item.get("query")).lower()
    if q in RULE_BY_QUERY: return RULE_BY_QUERY[q]
    title=normalize(item.get("title"))
    matches=[r for r in RULES if title_matches(r,title)]
    return matches[0] if len(matches)==1 else None

def format_alert(rule,item):
    title=normalize(item.get("title")); url=normalize(item.get("url"))
    price=as_price(item.get("price")); prev=as_price(item.get("previousPrice"))
    change=normalize(item.get("changeType")).lower()
    lines=[]
    if prev and prev>price: lines.append(f"📉 Было: {prev:,} ₽ → стало: <b>{price:,} ₽</b>")
    elif change=="new": lines.append("🆕 Новая карточка в выдаче")
    if item.get("isUnderpriced"): lines.append("🚩 Actor отметил цену как аномально низкую")
    extra=("\n".join(lines)+"\n") if lines else ""
    return (f"🔥 <b>{html.escape(rule['label'])}</b>\n"
            f"<b>{price:,} ₽</b> — наш порог: {rule['max_price']:,} ₽\n"
            f"{extra}{html.escape(title)}\n\n"
            f'<a href="{html.escape(url)}">Открыть товар на Ozon</a>\n\n'
            "⚠️ Перед оплатой проверь продавца, состояние, регион/ревизию, комплектацию, цену по Ozon Карте и возможную пошлину.").replace(","," ")

def main():
    apify=os.getenv("APIFY_TOKEN","").strip()
    tg=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
    missing=[n for n,v in [("APIFY_TOKEN",apify),("TELEGRAM_BOT_TOKEN",tg),("TELEGRAM_CHAT_ID",chat)] if not v]
    if missing: raise SystemExit("Missing GitHub Actions secret(s): "+", ".join(missing))

    state=load_state(); first=not state.get("apify_initialized")
    print("Starting Apify Ozon monitor...")
    items=run_apify(apify)
    print(f"Apify returned {len(items)} changed/new dataset item(s).")

    candidates=[]
    for item in items:
        if normalize(item.get("platform")).lower() not in ("","ozon"): continue
        rule=find_rule(item)
        if not rule: continue
        title=normalize(item.get("title")); price=as_price(item.get("price"))
        change=normalize(item.get("changeType")).lower()
        if not title or price is None or not title_matches(rule,title): continue
        if not (rule["min_price"] <= price <= rule["max_price"]): continue
        if change and change not in ("new","price_down"): continue
        candidates.append((price,rule,item))

    candidates.sort(key=lambda x:x[0])
    now=int(time.time()); sent=0
    for price,rule,item in candidates:
        if sent>=MAX_ALERTS_PER_RUN: break
        url=normalize(item.get("url"))
        identity=url or f"{rule['label']}|{normalize(item.get('title'))}"
        fp=hashlib.sha1(f"{identity}|{price}".encode()).hexdigest()
        if fp in state["alerts"]: continue
        send_telegram(tg,chat,format_alert(rule,item))
        state["alerts"][fp]=now; sent+=1
        print(f"Alert sent: {rule['label']} — {price} ₽ — {url}")

    if first:
        send_telegram(tg,chat,
            "✅ <b>Ozon-монитор подключён через Apify.</b>\n"
            f"Первый проход завершён. Actor вернул {len(items)} новых/изменённых карточек; "
            f"подходящих уведомлений отправлено: {sent}.")

    state["apify_initialized"]=True
    state["last_apify_run_unix"]=now
    state["last_apify_items"]=len(items)
    state["last_alerts_sent"]=sent
    save_state(state)
    print(f"Finished: {sent} alerts sent; {len(items)} changed/new item(s) returned by Apify.")

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
