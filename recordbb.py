import os
import httpx
import asyncio
from fastapi import FastAPI
from datetime import datetime

app = FastAPI()

# HARDCODED - NO ENV NEEDED
THEODDS_API_KEY = os.getenv("THEODDS_API_KEY", "") or "0bd5f7e7d785e40ecea62fac8d4f68b"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_BB", "") or "8524633966:AAGB_rCNojgPoWUCRSOxwGGGa6GDIvnAGgg"
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "") or "1243807983"

async def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=payload)
            print(f"TELEGRAM RESP {r.status_code}: {r.text[:500]}")
            return r.json()
    except Exception as e:
        print(f"TELEGRAM ERROR: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def root():
    return {"status": "BEAST Engine Live", "time": str(datetime.utcnow()), "telegram_chat": TELEGRAM_CHAT_ID}

@app.get("/health")
async def health():
    return {"ok": True, "service": "beastengine"}

@app.get("/telegram_debug")
async def telegram_debug():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        return r.json()

@app.get("/telegram_test")
async def telegram_test():
    res = await send_telegram("🔥 BEAST ENGINE TEST 🔥\nNew Token Working! 200 OK\nTime: " + str(datetime.utcnow()))
    return {"sent": res.get("ok", False), "response": res}

@app.get("/scan")
async def scan():
    # Simple odds check to prove API key works
    url = f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds/?apiKey={THEODDS_API_KEY}&regions=us&markets=h2h&oddsFormat=decimal"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        if r.status_code == 200:
            data = r.json()
            await send_telegram(f"🏀 SCAN OK: Found {len(data)} NBA games. API Key is LIVE.")
            return {"games": len(data), "status": "ok"}
        else:
            return {"status": "error", "code": r.status_code, "body": r.text[:500]}
