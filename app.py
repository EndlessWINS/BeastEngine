"""
app.py V4.3 - PRODUCTION + SQLite CLV + Telegram Alerts + TEST + /start - FINAL
BeastEngineFBBot | ID: 1243807983
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import sqlite3, json, os
from datetime import datetime

from match import (
    calculate_beast, get_top5_per_match, scan_all_matches,
    scan_prematch, scan_live, ALL_MARKETS, LEAGUE_CONFIG,
    calculate_clv
)

app = FastAPI(title="BEAST V4.3 PRODUCTION", version="4.3")

# ================= CONFIG - FINAL =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8714738524:AAGkBY6TBYsZUVwlHo6Ygy-7X6Av8tIomuQ")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1243807983")
DB_PATH = "beast_clv.db"
MIN_EV_ALERT = 8.0
MIN_CONF_ALERT = 0.70
# Change this to your Render URL!
RENDER_URL = os.getenv("RENDER_URL", "https://beastengine.onrender.com")

# ================= SQLITE INIT =================
def init_db():
    conn=sqlite3.connect(DB_PATH)
    c=conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS clv_tracker (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, fixture TEXT, league TEXT,
        market TEXT, taken REAL, close REAL, clv_percent REAL, ev_at_bet REAL,
        beat_close INTEGER, is_sharp INTEGER, kelly REAL, book_note TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS scan_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        total_fixtures INTEGER, total_bets INTEGER, results_json TEXT)""")
    conn.commit(); conn.close()
init_db()

def save_clv(rec):
    conn=sqlite3.connect(DB_PATH)
    c=conn.cursor()
    c.execute("INSERT INTO clv_tracker (timestamp,fixture,league,market,taken,close,clv_percent,ev_at_bet,beat_close,is_sharp,kelly,book_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), rec["fixture"], rec["league"], rec["market"], rec["taken"], rec["close"], rec["clv_percent"], rec["ev_at_bet"], int(rec["beat_close"]), int(rec["is_sharp"]), rec["kelly"], rec.get("book_note","")))
    conn.commit(); conn.close()

def get_clv_report_sql():
    conn=sqlite3.connect(DB_PATH)
    c=conn.cursor()
    c.execute("SELECT COUNT(*), AVG(clv_percent), AVG(ev_at_bet), SUM(beat_close), SUM(is_sharp) FROM clv_tracker")
    row=c.fetchone(); conn.close()
    if not row or row[0]==0: return {"total_bets":0,"verdict":"No bets"}
    total, avg_clv, avg_ev, beat, sharp = row
    return {
        "total_bets":total,
        "beat_close_rate": round((beat or 0)/total*100,2),
        "sharp_rate": round((sharp or 0)/total*100,2),
        "avg_clv_percent": round(avg_clv or 0,2),
        "avg_ev_percent": round(avg_ev or 0,2),
        "is_profitable_longterm": (avg_clv or 0)>1.5,
        "verdict": "SHARP - BEATING BOOKS" if (avg_clv or 0)>2 else "AVERAGE" if (avg_clv or 0)>0 else "LOSING"
    }

# ================= TELEGRAM =================
async def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            await client.post(url, json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"})
    except Exception as e: print(f"Telegram error: {e}")

async def reply_telegram(chat_id, msg):
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            await client.post(url, json={"chat_id":chat_id,"text":msg,"parse_mode":"Markdown"})
    except Exception as e: print(f"Reply error: {e}")

def format_alert(bets):
    txt=f"🔥 BEAST V4.2 ALERT - {len(bets)} VALUE BETS 🔥\n\n"
    for i,b in enumerate(bets[:5],1):
        txt+=f"{i}. {b['fixture']} | {b['league']}\n Market: {b['market']} EV:{b['ev_percent']}% Conf:{b['confidence']} Kelly:{b['kelly_pct']}%\n Odds: Pin {b['pinnacle_odds']} -> Soft {b['soft_odds']} | {b['book_note']}\n\n"
    return txt

# ================= MODELS =================
class OddsInput(BaseModel):
    league: str; home: str; away: str; market_type: str
    pinnacle_odds: float; soft_odds: float
    pinnacle_open: Optional[float]=None
    sibling_pin_odds: Optional[List[float]]=None
    multi_sources: Optional[List[Dict[str, Any]]]=None
    live_minute: Optional[int]=None
    live_red_card: Optional[str]=None
    live_score_home: Optional[int]=0
    live_score_away: Optional[int]=0

class BatchInput(BaseModel): odds: List[OddsInput]
class CLVInput(BaseModel):
    fixture: str; league: str; market: str
    taken: float; close: float; ev_at_bet: float=0; kelly: float=0
    closing_siblings: Optional[List[float]]=None

LAST_SCAN = {}; LAST_TOP_BETS = []

@app.get("/")
def root():
    return {"engine":"BEAST V4.3 PRODUCTION","markets":len(ALL_MARKETS),"leagues":len(LEAGUE_CONFIG),"clv":get_clv_report_sql(),"status":"ONLINE","bot":"@BeastEngineFBBot"}

# ================= NEW TEST ROUTES - ADDED =================
@app.get("/test")
async def test_alert():
    msg = "🔥 BEAST TEST ALERT - IT'S WORKING BABA! ✅\n\n⚽ Man City vs Arsenal\n🎯 Correct Score: 2-1\n📊 Confidence: 87%\n💰 Odds: 8.50\n📈 EV: +12.3%\n\nYour Football Beast Engine is LIVE and pushing to Telegram!\nbeastengine.onrender.com"
    await send_telegram(msg)
    return {"status": "Test sent to Telegram!", "bot": "@BeastEngineFBBot"}

@app.get("/set-webhook")
async def set_webhook():
    import httpx
    webhook_url = f"{RENDER_URL}/webhook"
    async with httpx.AsyncClient() as client:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
        r = await client.post(url, json={"url": webhook_url})
        return {"webhook_url": webhook_url, "telegram_response": r.json()}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    print(f"Webhook data: {data}")
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        if text == "/start":
            welcome = "👑 Welcome Baba! BEAST ENGINE V4.3 ONLINE!\n\n⚽ Football Beast Scanning:\n✅ 5 Leagues (Premier, La Liga, Bundesliga, Serie A, Ligue 1)\n✅ 10 Markets\n✅ EV > 8% | Conf > 70%\n\nYou go receive alert when correct score enter!\n\nCommands:\n/test - Test bot\n/clv - Check CLV report\n\nEngine: beastengine.onrender.com\nStatus: ONLINE 🔥"
            await reply_telegram(chat_id, welcome)
        elif text == "/clv":
            report = get_clv_report_sql()
            await reply_telegram(chat_id, f"📊 CLV REPORT:\n{json.dumps(report, indent=2)}")
    return {"ok": True}

# ================= EXISTING ROUTES =================
@app.post("/scan")
async def scan(data: BatchInput, mode: str="prematch", alert: bool=True):
    odds_list=[o.dict() for o in data.odds]
    results= scan_live(odds_list) if mode=="live" else scan_prematch(odds_list)
    global LAST_SCAN, LAST_TOP_BETS
    LAST_SCAN=results; LAST_TOP_BETS=[]
    for fixture,bets in results.items(): LAST_TOP_BETS.extend(bets)
    conn=sqlite3.connect(DB_PATH)
    c=conn.cursor()
    c.execute("INSERT INTO scan_history (timestamp,total_fixtures,total_bets,results_json) VALUES (?,?,?,?)",
        (datetime.now().isoformat(), len(results), len(LAST_TOP_BETS), json.dumps(results)[:50000]))
    conn.commit(); conn.close()
    high=[b for b in LAST_TOP_BETS if b["ev_percent"]>=MIN_EV_ALERT and b["confidence"]>=MIN_CONF_ALERT]
    if alert and high: await send_telegram(format_alert(high))
    return {"fixtures_with_value":len(results),"total_value_bets":len(LAST_TOP_BETS),"high_alerts":len(high),"results":results}

@app.post("/calculate")
def calc(data: OddsInput):
    res=calculate_beast(data.league,data.home,data.away,data.market_type,data.pinnacle_odds,data.soft_odds,data.pinnacle_open,data.sibling_pin_odds,data.multi_sources,data.live_minute,data.live_red_card,data.live_score_home,data.live_score_away)
    if not res: raise HTTPException(400,"Invalid")
    return res

@app.post("/clv/track")
def track(data: CLVInput):
    clv=calculate_clv(data.taken, data.close, data.closing_siblings)
    rec={"fixture":data.fixture,"league":data.league,"market":data.market,"taken":data.taken,"close":data.close,"clv_percent":clv["clv_percent"],"ev_at_bet":data.ev_at_bet,"beat_close":clv["beat_close"],"is_sharp":clv["is_sharp"],"kelly":data.kelly,"book_note":f"CLV {clv['clv_percent']}%"}
    save_clv(rec)
    return {"tracked":rec,"report":get_clv_report_sql()}

@app.get("/clv/report")
def report(): return get_clv_report_sql()

import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
