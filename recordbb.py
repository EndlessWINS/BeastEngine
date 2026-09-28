# recordbb.py - V8.2 HYBRID BEAST (production hardened for Render)
# Flow: ESPN free live check (cached 30s) -> if live: TheOdds snapshot (cached 10 min, ONE call
#       covers ALL games) + free adapters (every cycle) -> trap detection per live game
#       -> Telegram (deduped) + sqlite history.
#
# ENV VARS
#   THEODDS_API_KEY, TELEGRAM_BOT_TOKEN_BB, TELEGRAM_CHAT_ID
#   ADMIN_TOKEN            required to use /scan, /traps, /force-free, /force-premium (header: x-admin-token)
#   TRAP_THRESHOLD=5.0     points between sharp line and soft line
#   MIN_VOTES=2            soft books that must agree
#   TRAP_BOTH_SIDES=0      0 = original V8.1 (soft line LOWER than sharp only); 1 = also soft HIGHER
#   LOOP_INTERVAL=120      seconds between cycles
#   ODDS_TTL=600           TheOdds cache seconds (credit saver)
#   MIN_CREDITS=5          below this, premium is not called
#   ODDS_REGIONS=eu        each region = 1 credit per call
#   PREMIUM_BOOKS          TheOdds bookmaker keys to keep (pinnacle is the truth)
#   ADAPTER_MODULES=adapters.sporty,adapters.onex
#   ALERT_COOLDOWN=600     seconds before the same game/direction can alert again
#   DB_PATH=beast_traps.db (Render disk is ephemeral unless you attach a persistent disk)

import asyncio
import contextlib
import hmac
import html
import importlib
import inspect
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException

# ========= CONFIG =========
API_KEY = os.getenv("THEODDS_API_KEY", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_BB", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


TRAP_THRESHOLD = _f("TRAP_THRESHOLD", 5.0)
MIN_VOTES = int(_f("MIN_VOTES", 2))
TRAP_BOTH_SIDES = os.getenv("TRAP_BOTH_SIDES", "0") == "1"
LOOP_INTERVAL = _f("LOOP_INTERVAL", 120)
ESPN_TTL = _f("ESPN_TTL", 30)
ODDS_TTL = _f("ODDS_TTL", 600)
MIN_CREDITS = _f("MIN_CREDITS", 5)
ALERT_COOLDOWN = _f("ALERT_COOLDOWN", 600)
ADAPTER_TIMEOUT = _f("ADAPTER_TIMEOUT", 10)
LINE_MIN, LINE_MAX = _f("LINE_MIN", 150), _f("LINE_MAX", 320)  # sanity bounds for NBA totals
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "eu")
PREMIUM_BOOKS = {
    b.strip()
    for b in os.getenv(
        "PREMIUM_BOOKS",
        "pinnacle,onexbet,bet365,unibet_eu,unibet_uk,unibet_fr,unibet_it,unibet_nl,unibet_se",
    ).split(",")
    if b.strip()
}
ADAPTER_MODULES = [m.strip() for m in os.getenv("ADAPTER_MODULES", "adapters.sporty,adapters.onex").split(",") if m.strip()]
DB_PATH = os.getenv("DB_PATH", "beast_traps.db")

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"


# ========= LOGGING (secrets never reach logs) =========
class Redact(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
            for s in (API_KEY, BOT_TOKEN, ADMIN_TOKEN):
                if s:
                    msg = msg.replace(s, "***")
            record.msg, record.args = msg, ()
        except Exception:
            pass
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _h in logging.getLogger().handlers:
    _h.addFilter(Redact())
# httpx logs full request URLs at INFO - that includes apiKey and the Telegram bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("bb-beast")


def safe(e: Exception) -> str:
    s = f"{type(e).__name__}: {e}"
    for secret in (API_KEY, BOT_TOKEN, ADMIN_TOKEN):
        if secret:
            s = s.replace(secret, "***")
    return s[:300]


# ========= STATE =========
STATE = {
    "mode": "PREMIUM",
    "mode_override": None,       # None | "FREE" | "PREMIUM"
    "credits_left": None,        # unknown until the first TheOdds response header
    "last_free_check": None,
    "last_odds_check": None,
    "last_game": None,
    "is_live_now": False,
    "total_scans": 0,
    "engine": "V8.2_HYBRID",
}
HTTP: httpx.AsyncClient = None  # shared client, created in lifespan
CYCLE_LOCK = asyncio.Lock()
_espn = {"ts": 0.0, "games": [], "ok": False}
_espn_lock = asyncio.Lock()
_prem = {"ts": 0.0, "games": {}, "next_try": 0.0, "disabled_until": 0.0}
_prem_lock = asyncio.Lock()
_alerted = {}
ADAPTERS = {}


# ========= HELPERS =========
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def nick(name: str) -> str:
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return toks[-1] if toks else ""


def game_key(away: str, home: str) -> str:
    """Team-nickname key so ESPN / TheOdds / scrapers all map to the same game."""
    return f"{nick(away)}@{nick(home)}"


def period_label(p):
    try:
        p = int(p)
    except (TypeError, ValueError):
        return ""
    return f"Q{p}" if p <= 4 else f"OT{p - 4}"


# ========= 0. ESPN (free, cached, all games) =========
def parse_espn(data: dict) -> list:
    games = []
    for ev in data.get("events") or []:
        try:
            status = ev.get("status") or {}
            typ = status.get("type") or {}
            comp = (ev.get("competitions") or [{}])[0]
            home = away = ""
            hs = as_ = None
            for c in comp.get("competitors") or []:
                nm = (c.get("team") or {}).get("displayName") or ""
                if c.get("homeAway") == "home":
                    home, hs = nm, c.get("score")
                else:
                    away, as_ = nm, c.get("score")
            if not home or not away:
                continue
            state = typ.get("state", "")
            games.append({
                "id": ev.get("id"),
                "key": game_key(away, home),
                "home": home, "away": away,
                "home_score": hs, "away_score": as_,
                "state": state,
                "live": state == "in",
                "detail": typ.get("shortDetail", ""),
                "clock": status.get("displayClock", ""),
                "period": status.get("period"),
                "short": ev.get("shortName") or f"{away} @ {home}",
                "start": ev.get("date"),
            })
        except Exception:
            continue
    return games


async def get_espn_games():
    """Returns (games, ok). Cached ESPN_TTL seconds. A failure never touches TheOdds credits."""
    async with _espn_lock:
        if _espn["ok"] and time.monotonic() - _espn["ts"] < ESPN_TTL:
            return _espn["games"], True
        delay = 1.0
        for attempt in range(3):
            try:
                r = await HTTP.get(ESPN_URL, timeout=8)
                if r.status_code == 200:
                    _espn.update(ts=time.monotonic(), games=parse_espn(r.json()), ok=True)
                    return _espn["games"], True
                logger.warning("ESPN HTTP %s", r.status_code)
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("ESPN error: %s", safe(e))
            if attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
        _espn["ok"] = False
        return [], False


# ========= 1. PREMIUM: TheOdds (one call = every game, cached) =========
def parse_odds_events(events: list) -> dict:
    out = {}
    for ev in events:
        try:
            key = game_key(ev["away_team"], ev["home_team"])
            books = {}
            for bm in ev.get("bookmakers") or []:
                bk = bm.get("key")
                if bk not in PREMIUM_BOOKS:
                    continue
                for mk in bm.get("markets") or []:
                    if mk.get("key") != "totals":
                        continue
                    for o in mk.get("outcomes") or []:
                        if o.get("name") == "Over" and o.get("point") is not None:
                            line = float(o["point"])
                            if LINE_MIN <= line <= LINE_MAX:
                                books[bk] = {"line": line, "status": "PREMIUM"}
            if books:
                out[key] = books
        except (KeyError, TypeError, ValueError):
            continue
    return out


def premium_allowed() -> bool:
    if STATE["mode_override"] == "FREE":
        return False
    if not API_KEY:
        return False
    if time.time() < _prem["disabled_until"]:
        return False
    c = STATE["credits_left"]
    return c is None or c >= MIN_CREDITS


def current_mode() -> str:
    return "PREMIUM" if premium_allowed() else "FREE"


async def fetch_official_books():
    """Returns dict game_key -> {book: {...}} or None. Distinguishes 401 / 429 / 5xx / network."""
    params = {
        "apiKey": API_KEY, "regions": ODDS_REGIONS, "markets": "totals",
        "oddsFormat": "decimal", "dateFormat": "iso",
    }
    delay = 2.0
    for attempt in range(3):
        try:
            r = await HTTP.get(ODDS_URL, params=params, timeout=12)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning("TheOdds network error: %s", safe(e))
            if attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
            continue

        rem = r.headers.get("x-requests-remaining")
        if rem is not None:
            with contextlib.suppress(ValueError):
                STATE["credits_left"] = int(float(rem))

        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                return None
            return parse_odds_events(data) if isinstance(data, list) else None
        if r.status_code == 401:
            # invalid key OR out of credits: stop hitting it for 6h (does NOT flip to fake data)
            _prem["disabled_until"] = time.time() + 6 * 3600
            logger.error("TheOdds 401 (bad key or out of credits) - premium paused 6h")
            return None
        if r.status_code == 429:  # frequency limit, not quota
            try:
                wait = float(r.headers.get("retry-after", delay))
            except ValueError:
                wait = delay
            logger.warning("TheOdds 429 rate limit, waiting %.0fs", wait)
            if attempt < 2:
                await asyncio.sleep(min(wait, 30))
            continue
        if r.status_code >= 500:
            logger.warning("TheOdds HTTP %s", r.status_code)
            if attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
            continue
        logger.warning("TheOdds HTTP %s: %s", r.status_code, r.text[:150])
        _prem["next_try"] = time.time() + 300
        return None
    return None


async def get_premium_snapshot():
    """Returns (snapshot|None, fresh). At most ONE TheOdds call per ODDS_TTL, however many games are live."""
    async with _prem_lock:
        now = time.time()
        if _prem["ts"] and now - _prem["ts"] < ODDS_TTL:
            return _prem["games"], False
        if not premium_allowed() or now < _prem["next_try"]:
            return None, False
        games = await fetch_official_books()
        if games is None:
            _prem["next_try"] = time.time() + 60  # don't hammer after a failure
            return None, False
        _prem.update(ts=time.time(), games=games)
        STATE["last_odds_check"] = now_iso()
        return games, True


# ========= 2. FREE ADAPTERS =========
# Contract (new):    async def fetch(client, games) -> list[{"home","away","line","book"}]
# Contract (legacy): async def fetch() -> {"line": 224.5, "book": "sporty"}  (only used when exactly 1 game is live)
def load_adapters():
    ADAPTERS.clear()
    for modname in ADAPTER_MODULES:
        try:
            fn = getattr(importlib.import_module(modname), "fetch")
            ADAPTERS[modname.split(".")[-1]] = {"fn": fn, "fails": 0, "skip_until": 0.0}
            logger.info("Adapter loaded: %s", modname)
        except Exception as e:
            logger.info("Adapter %s not loaded (%s)", modname, type(e).__name__)


def normalize_adapter_result(name, res, live_games):
    items = res if isinstance(res, list) else ([res] if isinstance(res, dict) else [])
    out = []
    for it in items:
        try:
            line = float(it.get("line"))
            book = str(it.get("book") or name).lower()
        except (TypeError, ValueError, AttributeError):
            continue
        if not LINE_MIN <= line <= LINE_MAX:
            continue
        if it.get("home") and it.get("away"):
            key = game_key(it["away"], it["home"])
        elif len(live_games) == 1:
            key = live_games[0]["key"]
        else:
            continue  # can't tell which game a team-less line belongs to
        out.append((key, book, line))
    return out


async def run_adapter(name, ad, live_games):
    if time.time() < ad["skip_until"]:
        return []
    delay = 1.0
    for attempt in range(3):
        try:
            fn = ad["fn"]
            coro = fn() if len(inspect.signature(fn).parameters) == 0 else fn(HTTP, live_games)
            res = await asyncio.wait_for(coro, ADAPTER_TIMEOUT)
            ad["fails"] = 0
            return normalize_adapter_result(name, res, live_games)
        except Exception as e:
            logger.warning("Adapter %s attempt %d failed: %s", name, attempt + 1, safe(e))
            if attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
    ad["fails"] += 1
    if ad["fails"] >= 3:  # circuit breaker
        ad["skip_until"] = time.time() + 300
        logger.warning("Adapter %s paused 5 min", name)
    return []


async def fetch_free_books(live_games):
    """game_key -> {book: {line, status}}. NO fake placeholder data: no adapters = no free signal."""
    if not ADAPTERS:
        return {}
    results = await asyncio.gather(*[run_adapter(n, a, live_games) for n, a in ADAPTERS.items()])
    by_game = {}
    for res in results:
        for key, book, line in res:
            by_game.setdefault(key, {})[book] = {"line": line, "status": "FREE"}
    return by_game


# ========= 3. TRAP DETECTION =========
def evaluate(books: dict, truth_key: str) -> dict:
    base = {"books": books, "truth_book": truth_key, "threshold": TRAP_THRESHOLD}
    truth = (books.get(truth_key) or {}).get("line")
    if truth is None:
        return {**base, "status": "NO_TRUTH_SKIP", "is_trap": False}
    soft = {k: v["line"] for k, v in books.items() if k != truth_key}
    if not soft:
        return {**base, "status": "NO_SOFT_BOOKS_SKIP", "truth_line": truth, "is_trap": False}

    over = [k for k, l in soft.items() if truth - l >= TRAP_THRESHOLD]  # soft total LOWER than sharp -> OVER
    under = [k for k, l in soft.items() if l - truth >= TRAP_THRESHOLD] if TRAP_BOTH_SIDES else []
    voters, direction = (over, "OVER") if len(over) >= len(under) else (under, "UNDER")
    is_trap = len(voters) >= MIN_VOTES
    return {
        **base, "status": "REAL_LIVE_ODDS", "truth_line": truth, "soft_count": len(soft),
        "trap_votes": len(voters), "voters": voters,
        "direction": direction if voters else None,
        "is_trap": is_trap, "action": "BET_TRAP" if is_trap else "SKIP",
    }


# ========= TELEGRAM (POST, escaped, never raises) =========
async def send_telegram(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        logger.info("Telegram not configured; would send: %s", msg[:100].replace("\n", " | "))
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            r = await HTTP.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                await asyncio.sleep(min(wait, 30))
                continue
            logger.warning("Telegram HTTP %s", r.status_code)
            return
        except httpx.HTTPError as e:
            logger.warning("Telegram error: %s", safe(e))
            await asyncio.sleep(2 ** attempt)


def format_trap(g: dict, res: dict) -> str:
    e = html.escape
    truth = res["truth_line"]
    soft_lines = []
    for k in res["voters"]:
        ln = res["books"][k]["line"]
        soft_lines.append(f"{e(k)} {ln} ({ln - truth:+.1f})")
    score = f"{e(g['away'])} {g['away_score'] or '-'} - {e(g['home'])} {g['home_score'] or '-'}"
    when = f"{period_label(g['period'])} {e(g['clock'] or '')}".strip() or e(g["detail"])
    credits = STATE["credits_left"] if STATE["credits_left"] is not None else "n/a"
    return (
        f"🔥 <b>BB TRAP - {res['mode']}</b>\n"
        f"{e(g['away'])} @ {e(g['home'])}\n"
        f"⏱ {when} | {score}\n"
        f"Sharp ({e(res['truth_book'])}): <b>{truth}</b>\n"
        f"Soft: {', '.join(soft_lines)}\n"
        f"Signal: <b>{res['direction']}</b> on soft books | votes {res['trap_votes']}/{res['soft_count']}\n"
        f"Credits left: {credits}"
    )


# ========= SQLITE TRAP HISTORY =========
def db_init():
    with closing(sqlite3.connect(DB_PATH)) as c, c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS traps(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, game TEXT, mode TEXT,"
            " truth_book TEXT, truth_line REAL, direction TEXT, votes INTEGER, books TEXT, period TEXT,"
            " clock TEXT, score TEXT, credits REAL)"
        )


def db_insert(row: tuple):
    with closing(sqlite3.connect(DB_PATH)) as c, c:
        c.execute("INSERT INTO traps(ts,game,mode,truth_book,truth_line,direction,votes,books,period,clock,score,credits)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)


def db_recent(limit: int):
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM traps ORDER BY id DESC LIMIT ?", (limit,))]


async def handle_trap(g: dict, res: dict):
    ck = f"{g['key']}|{res['direction']}|{res['mode']}"
    now = time.time()
    for k in [k for k, t in _alerted.items() if now - t > ALERT_COOLDOWN]:
        del _alerted[k]
    if ck in _alerted:
        res["alert"] = "DUPLICATE_SUPPRESSED"
        return
    _alerted[ck] = now
    row = (now_iso(), g["short"], res["mode"], res["truth_book"], res["truth_line"], res["direction"],
           res["trap_votes"], json.dumps(res["books"]), str(g["period"] or ""), g["clock"] or "",
           f"{g['away_score']}-{g['home_score']}", STATE["credits_left"] or -1)
    try:
        await asyncio.to_thread(db_insert, row)
    except Exception as e:
        logger.warning("DB write failed: %s", safe(e))
    await send_telegram(format_trap(g, res))
    res["alert"] = "SENT"


# ========= CYCLE =========
async def run_cycle():
    async with CYCLE_LOCK:
        STATE["total_scans"] += 1
        STATE["last_free_check"] = now_iso()
        games, ok = await get_espn_games()
        if not ok:
            return {"status": "ESPN_UNAVAILABLE_SKIP_0_CREDIT"}
        live = [g for g in games if g["live"]]
        STATE["is_live_now"] = bool(live)
        STATE["last_game"] = ", ".join(g["short"] for g in live) or None
        STATE["mode"] = current_mode()
        if not live:
            return {"status": "NO_GAMES_LIVE_SKIP_0_CREDIT"}

        live_by_key = {g["key"]: g for g in live}
        results = []

        # Premium: evaluate the snapshot once, when it is fresh (all games from one call)
        snap, fresh = await get_premium_snapshot()
        if snap is not None and fresh:
            for key, g in live_by_key.items():
                books = snap.get(key)
                if not books:
                    results.append({"game": g["short"], "status": "NO_ODDS_FOR_GAME", "mode": "PREMIUM"})
                    continue
                res = evaluate(books, "pinnacle")
                res.update(mode="PREMIUM", game=g["short"])
                results.append(res)
                if res["is_trap"]:
                    await handle_trap(g, res)

        # Free: every cycle, truth = bet365 from the free feed
        free = await fetch_free_books(live)
        for key, books in free.items():
            g = live_by_key.get(key)
            if not g or len(books) < 2:
                continue
            res = evaluate(books, "bet365")
            res.update(mode="FREE", game=g["short"])
            results.append(res)
            if res["is_trap"]:
                await handle_trap(g, res)

        return {"status": "OK", "live_games": len(live), "premium_fresh": bool(fresh),
                "results": results, "credits_left": STATE["credits_left"], "mode": current_mode()}


async def background_loop():
    await asyncio.sleep(10)
    logger.info("Background loop started (interval %.0fs)", LOOP_INTERVAL)
    while True:
        try:
            r = await run_cycle()
            logger.info("Cycle: %s", r.get("status"))
        except Exception as e:
            logger.error("Loop error: %s", safe(e))
        await asyncio.sleep(LOOP_INTERVAL)


# ========= APP =========
@asynccontextmanager
async def lifespan(app: FastAPI):
    global HTTP
    HTTP = httpx.AsyncClient(
        timeout=httpx.Timeout(12, connect=6),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        headers={"User-Agent": "Mozilla/5.0 (compatible; bb-beast/8.2)"},
    )
    try:
        db_init()
    except Exception as e:
        logger.warning("DB init failed: %s", safe(e))
    load_adapters()
    if not API_KEY:
        logger.warning("THEODDS_API_KEY not set - premium disabled")
    task = asyncio.create_task(background_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await HTTP.aclose()


app = FastAPI(title="BB Beast V8.2 Hybrid", lifespan=lifespan)


def require_admin(x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN:
        raise HTTPException(403, "ADMIN_TOKEN not configured")
    if not hmac.compare_digest(x_admin_token.encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(401, "bad token")


@app.get("/")
async def home():
    return {"message": "BB Beast V8.2 Hybrid", "engine": STATE["engine"], "mode": current_mode()}


@app.get("/health")
async def health():
    return {"status": "ok", "last_free_check": STATE["last_free_check"], "scans": STATE["total_scans"]}


@app.get("/status")
async def status():
    now = time.time()
    return {
        **STATE, "mode": current_mode(),
        "premium_cache_age_s": round(now - _prem["ts"]) if _prem["ts"] else None,
        "premium_paused_until": _prem["disabled_until"] if _prem["disabled_until"] > now else None,
        "adapters": {n: {"fails": a["fails"], "paused": a["skip_until"] > now} for n, a in ADAPTERS.items()},
        "trap_threshold": TRAP_THRESHOLD, "min_votes": MIN_VOTES,
    }


@app.get("/games")
async def games_endpoint():
    """All NBA games today with live status; uses cached ESPN + cached premium lines (0 credits)."""
    games, ok = await get_espn_games()
    out = []
    for g in games:
        lines = _prem["games"].get(g["key"]) if _prem["ts"] else None
        out.append({
            "game": g["short"], "state": g["state"], "live": g["live"],
            "period": period_label(g["period"]), "clock": g["clock"], "detail": g["detail"],
            "score": f"{g['away_score']}-{g['home_score']}", "start": g["start"],
            "cached_lines": {k: v["line"] for k, v in lines.items()} if lines else None,
        })
    return {"espn_ok": ok, "count": len(out), "games": out}


@app.get("/scan", dependencies=[Depends(require_admin)])
async def manual_scan():
    if CYCLE_LOCK.locked():
        return {"status": "SCAN_ALREADY_RUNNING"}
    return await run_cycle()


@app.get("/traps", dependencies=[Depends(require_admin)])
async def traps(limit: int = 50):
    return {"traps": await asyncio.to_thread(db_recent, max(1, min(limit, 500)))}


@app.post("/force-free", dependencies=[Depends(require_admin)])
async def force_free():
    STATE["mode_override"] = "FREE"
    return {"forced_to": "FREE"}


@app.post("/force-premium", dependencies=[Depends(require_admin)])
async def force_premium():
    STATE["mode_override"] = None
    _prem["disabled_until"] = 0.0
    _prem["next_try"] = 0.0
    return {"forced_to": "PREMIUM (auto)", "mode": current_mode()}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
