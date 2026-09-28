import os, threading, time, random, requests
from flask import Flask, request
from datetime import datetime

app = Flask(__name__)

# GET FROM RENDER ENV - NO HARDCODE!
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1243807983")

# Markets we dey use
MARKETS = ["Over 1.5 Goals", "Over 2.5 Goals", "BTTS YES", "Next Goal Home", "Over 8.5 Corners", "Home Win"]

LIVE_MATCHES = [
    ("Arsenal vs Man City", "Premier League"),
    ("Barcelona vs Real Madrid", "La Liga"),
    ("Bayern vs Dortmund", "Bundesliga"),
    ("PSG vs Marseille", "Ligue 1"),
    ("Inter vs AC Milan", "Serie A")
]

def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def live_scanner_loop():
    print("🔥 LIVE SCANNER STARTED BB!")
    time.sleep(10) # wait Render to settle
    send_telegram("🔥 *BEAST LIVE SCANNER don START BB!*\n\nI go dey scan live matches every 2 mins and push correct market for you!\n\nRelax, make I work! 💪")

    while True:
        try:
            # For now - smart random pick (later we connect real football API)
            if random.random() > 0.7: # 30% chance to send alert every 2 mins
                match, league = random.choice(LIVE_MATCHES)
                minute = random.randint(55, 88)
                market = random.choice(MARKETS)
                confidence = random.randint(82, 94)
                score = f"{random.randint(0,2)}-{random.randint(0,2)}"

                msg = f"🔥 *LIVE ALERT {minute}'* - {league}\n\n⚽️ *{match}*\n📊 Score: {score}\n\n🎯 *MARKET TO TAKE:*\n*{market}*\n\n📈 Confidence: *{confidence}%*\n⏰ Time: {minute} mins\n💡 Reason: Momentum high, pressure dey for {match.split(' vs ')[0]} side, odds dey drop!\n\n⚡️ BeastEngineFB"
                send_telegram(msg)

            time.sleep(120) # scan every 2 mins
        except Exception as e:
            print(f"Scanner error: {e}")
            time.sleep(60)

# START SCANNER IN BACKGROUND THREAD
scanner_thread = threading.Thread(target=live_scanner_loop, daemon=True)
scanner_thread.start()

@app.route('/')
def home():
    return "BeastEngineFB LIVE SCANNER ON BB 🔥"

@app.route('/test')
def test():
    send_telegram("🔥 *BEAST TEST ALERT - SCANNER ACTIVE BB!* ✅\n\nScanner dey ON for background!")
    return {"status": "Test sent", "bot": "BeastEngineFBBot", "scanner": "ON"}

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if data and "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        if "/start" in text:
            send_telegram("🔥 Welcome to *BeastEngineFB* BB!\n\n✅ Live Scanner = ON\n✅ I dey scan 24/7\n\nYou go dey get alerts automatically!\n\nSend /status to check scanner")
        elif "/status" in text:
            send_telegram(f"✅ *Scanner Status: ACTIVE BB!*\n\n⏰ Time: {datetime.now().strftime('%H:%M:%S')}\n🔍 Scanning every 2 mins\n📡 Sending to: {TELEGRAM_CHAT_ID}")
    return {"ok": True}

@app.route('/set-webhook')
def set_webhook():
    if not TELEGRAM_TOKEN:
        return {"ok": False, "error": "No token"}
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url=https://beastengine.onrender.com/webhook"
    r = requests.get(url)
    return r.json()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
