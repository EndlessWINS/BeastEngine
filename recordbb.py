# BEAST V2 - WORLD RECORD BB - KILL THEM ALL ENGINE (FIXED)
# Purpose: Scan ALL leagues in parallel, devig Pinnacle, kill soft-book mispricings
# For: SportyBet & 1xBet
#
# ENV VARS (Render):
# THEODDS_API_KEY required
# TELEGRAM_BOT_TOKEN_BB optional
# TELEGRAM_CHAT_ID optional
# SCAN_INTERVAL seconds between scans (default 120)
# ODDS_REGIONS default "eu" (add ",uk" if you need UK books; doubles credit cost)
# SOFT_BOOKS comma list of TheOddsAPI bookmaker keys
# INCLUDE_LIVE "1" to include games already started (default 0)
# MIN_CREDITS stop scanning when API credits remaining < this (default 25)
# LOG_FILE default beast_bb_log.csv

import asyncio
import contextlib
import csv
import html
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

# ========= CONFIG =========
API_KEY = os.getenv("THEODDS_API_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_BB", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_FILE = Path(os.getenv("LOG_FILE", "beast_bb_log.csv"))

BASE = "https://api.the-odds-api.com/v4"
REGIONS = os.getenv("ODDS_REGIONS", "eu")
SHARP_BOOK = "pinnacle"
SOFT_BOOKS = [
    b.strip()
    for b in os.getenv(
        "SOFT_BOOKS",
        "onexbet,bet365,unibet_eu,unibet_uk,unibet_fr,unibet_it,unibet_nl,unibet_se",
    ).split(",")
    if b.strip()
]
SOFT_SET = set(SOFT_BOOKS)

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "5"))
MIN_CREDITS = float(os.getenv("MIN_CREDITS", "25"))
INCLUDE_LIVE = os.getenv("INCLUDE_LIVE", "0") == "1"

# World-record filter
MIN_EV_PCT = 3.0
MIN_CONF = 54.0
MIN_MUST_WIN = 0.58
MIN_SOFT_ODDS = 1.20
MAX_EV_PCT = 20.0

BIAS_WEIGHT = float(os.getenv("BIAS_WEIGHT", "0.0025"))

LOW_LIQUIDITY = {"nigeria", "bal", "philippines", "fiba_intercontinental", "nba_gleague"}
LOW_LIQ_EXTRA_EV = 2.0
LOW_LIQ_EXTRA_CONF = 2.0
MAX_OVERROUND = 1.08
MAX_OVERROUND_LOW = 1.06

TIER_BOOST = {"NBA_FAST": 1.10}
BOOK_BOOST = {"onexbet": 1.10, "bet365": 1.10}

DEDUP_SECONDS = 600
MAX_ALERTS_PER_SCAN = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("beast_bb")

LEAGUE_CONFIG = {
    "nba": {"bias": 14, "q_len": 720, "avg": 56, "tier": "NBA_FAST", "api": "basketball_nba"},
    "nba_gleague": {"bias": 11, "q_len": 720, "avg": 53, "tier": "NBA_FAST", "api": "basketball_nba_gleague"},
    "philippines": {"bias": 6, "q_len": 600, "avg": 48, "tier": "HIGH", "api": "basketball_philippines_pba"},
    "nigeria": {"bias": 5, "q_len": 600, "avg": 46, "tier": "HIGH", "api": "basketball_nigeria_premier"},
    "fiba_intercontinental": {"bias": 5, "q_len": 600, "avg": 45, "tier": "HIGH", "api": "basketball_fiba_intercontinental"},
    "china": {"bias": 4, "q_len": 600, "avg": 44, "tier": "HIGH", "api": "basketball_china_cba"},
    "japan": {"bias": 4, "q_len": 600, "avg": 44, "tier": "HIGH", "api": "basketball_japan_b1"},
    "bal": {"bias": 4, "q_len": 600, "avg": 43, "tier": "HIGH", "api": "basketball_bal"},
    "nbl": {"bias": 3, "q_len": 600, "avg": 42, "tier": "MED_HIGH", "api": "basketball_australia_nbl"},
    "korea": {"bias": 3, "q_len": 600, "avg": 41, "tier": "MED_HIGH", "api": "basketball_korea_kbl"},
    "wnba": {"bias": 2, "q_len": 600, "avg": 41, "tier": "MED_HIGH", "api": "basketball_wnba"},
    "spain": {"bias": 1, "q_len": 600, "avg": 40, "tier": "EURO_BASE", "api": "basketball_spain_acb"},
    "germany": {"bias": 1, "q_len": 600, "avg": 40, "tier": "EURO_BASE", "api": "basketball_germany_bbl"},
    "euroleague": {"bias": 0, "q_len": 600, "avg": 39, "tier": "EURO_BASE", "api": "basketball_euroleague"},
    "eurocup": {"bias": 0, "q_len": 600, "avg": 39, "tier": "EURO_BASE", "api": "basketball_eurocup"},
    "italy": {"bias": -1, "q_len": 600, "avg": 38, "tier": "EURO_SLOW", "api": "basketball_italy_lega_a"},
    "turkey": {"bias": -2, "q_len": 600, "avg": 37, "tier": "EURO_SLOW", "api": "basketball_turkey_bsl"},
    "greece": {"bias": -2, "q_len": 600, "avg": 37, "tier": "EURO_SLOW", "api": "basketball_greece_gbl"},
    "france": {"bias": -3, "q_len": 600, "avg": 36, "tier": "EURO_SLOW", "api": "basketball_france_lnb"},
}

class State:
    def __init__(self):
        self.credits = None
        self.last_scan = None
        self.last_kills = []
        self.active_keys = None
        self.active_ts = 0.0
        self.lock = asyncio.Lock()

STATE = State()
SEEN_ALERTS = {}
CSV_FIELDS = [
    "timestamp", "league", "tier", "match", "start", "market", "book",
    "soft_odds", "fair_prob", "ev_pct", "conf", "must_win", "rank_score",
]

async def api_get(client: httpx.AsyncClient, url: str, params: dict, retries: int = 4):
    delay = 1.0
    for attempt in range(retries + 1):
        wait = delay
        try:
            r = await client.get(url, params=params, timeout=20)
            rem = r.headers.get("x-requests-remaining")
            if rem is not None:
                with contextlib.suppress(ValueError):
                    STATE.credits = float(rem)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    log.warning("Bad JSON from %s", url)
                    return None
            if r.status_code in (401, 403):
                log.error("API auth/quota error %s", r.status_code)
                return None
            if r.status_code in (404, 422):
                return None
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after", "")
                with contextlib.suppress(ValueError):
                    wait = float(ra)
                log.warning("HTTP %s, retry %d in %.1fs", r.status_code, attempt + 1, wait)
            else:
                log.warning("HTTP %s", r.status_code)
                return None
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("Network error (%s), retry %d", type(e).__name__, attempt + 1)
        if attempt < retries:
            await asyncio.sleep(wait + random.uniform(0, 0.5))
            delay = min(delay * 2, 30)
    return None

async def get_active_keys(client: httpx.AsyncClient):
    if STATE.active_keys is not None and time.time() - STATE.active_ts < 1800:
        return STATE.active_keys
    data = await api_get(client, f"{BASE}/sports/", {"apiKey": API_KEY})
    if isinstance(data, list):
        STATE.active_keys = {s["key"] for s in data if s.get("active") and "key" in s}
        STATE.active_ts = time.time()
    return STATE.active_keys

def build_sharp_map(event: dict) -> dict:
    fair = {}
    for bm in event.get("bookmakers", []):
        if bm.get("key")!= SHARP_BOOK:
            continue
        for mk in bm.get("markets", []):
            outs = [o for o in mk.get("outcomes", []) if o.get("price") and o["price"] > 1.0]
            if len(outs)!= 2:
                continue
            inv = [1.0 / o["price"] for o in outs]
            total = sum(inv)
            for o, i in zip(outs, inv):
                fair[(mk["key"], o["name"], o.get("point"))] = (i / total, total)
    return fair

def apply_bias(prob: float, cfg: dict, market_key: str, outcome_name: str) -> float:
    if market_key!= "totals":
        return prob
    bias = cfg["bias"]
    shift = abs(bias) * BIAS_WEIGHT
    if outcome_name == "Under" and bias > 0:
        prob -= shift
    elif outcome_name == "Over" and bias < 0:
        prob -= shift
    return max(0.05, min(0.95, prob))

def win_market_score(real_prob, soft_odds, league_cfg, market_key, outcome_name):
    real_prob = apply_bias(real_prob, league_cfg, market_key, outcome_name)
    ev = real_prob * soft_odds - 1
    must_win_score = real_prob * (1 + ev)
    confidence = round(real_prob * 100, 1)
    return ev, must_win_score, confidence, real_prob

def has_started(event: dict) -> bool:
    ct = event.get("commence_time")
    if not ct:
        return False
    try:
        dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt <= datetime.now(timezone.utc)

async def send_telegram(client: httpx.AsyncClient, text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("[TELEGRAM SKIP]\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            r = await client.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                await asyncio.sleep(min(wait, 30))
                continue
            return
        except Exception as e:
            log.warning("Telegram error: %s", e)
            await asyncio.sleep(2 ** attempt)

def format_alerts(kills: list) -> list:
    header = f"🏀 <b>BB BEAST - {len(kills)} KILL(S)</b> 🏀\n"
    chunks, cur = [], header
    for k in kills:
        block = (
            f"\n<b>{html.escape(k['league'].upper())}</b> [{k['tier']}]\n"
            f"{html.escape(k['match'])}\n"
            f"{html.escape(k['market'])}\n"
            f"{html.escape(str(k['book']))} @ <b>{k['soft_odds']}</b>\n"
            f"EV {k['ev']}% | CONF {k['conf']}% | RANK {k['rank_score']}\n"
        )
        if len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = header
        cur += block
    chunks.append(cur + "\n🔥 GO COLLECT THE MONIES")
    return chunks

def write_csv(rows: list):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})

def is_duplicate(key: str) -> bool:
    now = time.time()
    for k in [k for k, t in SEEN_ALERTS.items() if now - t > DEDUP_SECONDS]:
        del SEEN_ALERTS[k]
    if key in SEEN_ALERTS:
        return True
    SEEN_ALERTS[key] = now
    return False

async def scan_league(client: httpx.AsyncClient, league_key: str, cfg: dict) -> list:
    events = await api_get(
        client,
        f"{BASE}/sports/{cfg['api']}/odds/",
        {"apiKey": API_KEY, "regions": REGIONS, "markets": "h2h,spreads,totals", "oddsFormat": "decimal", "dateFormat": "iso"},
    )
    if not isinstance(events, list) or not events:
        return []
    low = league_key in LOW_LIQUIDITY
    min_ev = MIN_EV_PCT + (LOW_LIQ_EXTRA_EV if low else 0.0)
    min_conf = MIN_CONF + (LOW_LIQ_EXTRA_CONF if low else 0.0)
    max_over = MAX_OVERROUND_LOW if low else MAX_OVERROUND
    kills = []
    for event in events:
        if not INCLUDE_LIVE and has_started(event):
            continue
        fair = build_sharp_map(event)
        if not fair:
            continue
        home, away = event.get("home_team", "?"), event.get("away_team", "?")
        for bm in event.get("bookmakers", []):
            bkey = bm.get("key")
            if bkey not in SOFT_SET:
                continue
            for mk in bm.get("markets", []):
                for out in mk.get("outcomes", []):
                    odds = out.get("price")
                    if not odds or odds < MIN_SOFT_ODDS:
                        continue
                    f = fair.get((mk["key"], out["name"], out.get("point")))
                    if not f:
                        continue
                    prob, overround = f
                    if overround > max_over:
                        continue
                    ev, must_win, conf, real_adj = win_market_score(prob, odds, cfg, mk["key"], out["name"])
                    ev_pct = ev * 100
                    if not (min_ev <= ev_pct <= MAX_EV_PCT and conf >= min_conf and must_win >= MIN_MUST_WIN):
                        continue
                    boost = TIER_BOOST.get(cfg["tier"], 1.0) * BOOK_BOOST.get(bkey, 1.0)
                    point = out.get("point")
                    kills.append({
                        "id": f"{event.get('id')}|{mk['key']}|{out['name']}|{point}|{bkey}",
                        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "league": league_key,
                        "tier": cfg["tier"],
                        "match": f"{away} vs {home}",
                        "start": event.get("commence_time", ""),
                        "market": f"{mk['key']} {out['name']}" + (f" {point}" if point is not None else ""),
                        "book": bm.get("title", bkey),
                        "soft_odds": round(odds, 3),
                        "fair_prob": round(real_adj, 4),
                        "ev": round(ev_pct, 2),
                        "ev_pct": round(ev_pct, 2),
                        "must_win": round(must_win, 4),
                        "conf": conf,
                        "rank_score": round(ev_pct * conf * boost, 1),
                    })
    return kills

async def BEAST_V2_KILL_ALL() -> dict:
    if not API_KEY:
        log.error("THEODDS_API_KEY is not set")
        return {"kills": [], "note": "THEODDS_API_KEY missing"}
    async with STATE.lock:
        log.info("BB BEAST V2 - KILL THEM ALL START")
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient() as client:
            active = await get_active_keys(client)
            targets = [(k, c) for k, c in LEAGUE_CONFIG.items() if active is None or c["api"] in active]
            async def guarded(k, c):
                async with sem:
                    if STATE.credits is not None and STATE.credits < MIN_CREDITS:
                        return []
                    try:
                        return await scan_league(client, k, c)
                    except Exception as e:
                        log.exception("Error scanning %s: %s", k, e)
                        return []
            results = await asyncio.gather(*[guarded(k, c) for k, c in targets])
            all_kills = [k for res in results for k in res]
            all_kills.sort(key=lambda k: k["rank_score"], reverse=True)
            fresh = [k for k in all_kills if not is_duplicate(k["id"])]
            if fresh:
                try:
                    await asyncio.to_thread(write_csv, fresh)
                except Exception as e:
                    log.warning("CSV write failed: %s", e)
                for chunk in format_alerts(fresh[:MAX_ALERTS_PER_SCAN]):
                    await send_telegram(client, chunk)
        STATE.last_scan = datetime.now(timezone.utc).isoformat(timespec="seconds")
        STATE.last_kills = all_kills
        log.info("Scanned %d/%d | kills=%d new=%d | credits=%s", len(targets), len(LEAGUE_CONFIG), len(all_kills), len(fresh), STATE.credits)
        return {"kills": all_kills, "new": len(fresh), "leagues_scanned": len(targets)}

async def BEAST_LOOP():
    await asyncio.sleep(5)
    while True:
        try:
            await BEAST_V2_KILL_ALL()
        except Exception as e:
            log.exception("Loop error: %s", e)
        await asyncio.sleep(SCAN_INTERVAL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(BEAST_LOOP())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

app = FastAPI(title="BB BEAST V2 - WORLD RECORD", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "BB BEAST V2 - WORLD RECORD - KILL THEM ALL MODE", "leagues": len(LEAGUE_CONFIG)}

@app.get("/health")
async def health():
    return {"status": "ok", "last_scan": STATE.last_scan, "credits_remaining": STATE.credits, "last_kill_count": len(STATE.last_kills)}

@app.get("/scan")
async def scan_once():
    if STATE.lock.locked():
        return {"status": "scan already running", "time": datetime.now().isoformat()}
    res = await BEAST_V2_KILL_ALL()
    return {**res, "time": datetime.now().isoformat()}

@app.get("/kills")
async def kills():
    return {"last_scan": STATE.last_scan, "kills": STATE.last_kills}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
