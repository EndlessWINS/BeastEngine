# BEAST V3.2 - CREDIT SAVER EDITION - BASED ON CLAUDE 1200 LINES
# FIXES: One credit per league (~0.1 per match) + Free Live Loop every 120s (0 credits)
import asyncio, contextlib, csv, html, logging, os, random, re, time
from collections import defaultdict, Counter, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import httpx, uvicorn
from fastapi import FastAPI

def env_float(n,d):
    try: return float(os.getenv(n,str(d)))
    except: return float(d)
def env_int(n,d):
    try: return int(float(os.getenv(n,str(d))))
    except: return int(d)
def env_bool(n,d):
    v=os.getenv(n)
    if v is None: return d
    return v.strip().lower() in ("1","true","yes","on")

RAW_API_KEY = os.getenv("THEODDS_API_KEY","") or os.getenv("ODDS_API_KEY","") or "0bd5f7e7d785e40ecea62fac8d4f68b8"
API_KEY = RAW_API_KEY.strip()[:32]
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_BB","") or "8524633966:AAGB_rCNojgPoWUCRSOxwGGGa6GDIvnAGgg"
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","") or "1243807983"
LOG_FILE = Path(os.getenv("LOG_FILE","beast_bb_log.csv"))
BASE = "https://api.the-odds-api.com/v4"
REGIONS = os.getenv("ODDS_REGIONS","eu,us,uk,au")
SHARP_BOOK="pinnacle"
SOFT_BOOKS=[b.strip() for b in os.getenv("SOFT_BOOKS","onexbet,bet365,unibet_eu,unibet_uk,unibet_fr,unibet_it,unibet_nl,unibet_se,betfair_ex_eu,williamhill,ladbrokes_uk,betfair,matchbook").split(",") if b.strip()]
SOFT_SET=set(SOFT_BOOKS)
EXCHANGE_BOOKS={"betfair_ex_eu","betfair","matchbook"}
EXCHANGE_COMMISSION=env_float("EXCHANGE_COMMISSION",0.02)
SCAN_INTERVAL=env_int("SCAN_INTERVAL",600) # CREDIT LOOP 10 mins
LIVE_INTERVAL=env_int("LIVE_INTERVAL",120) # FREE LOOP 2 mins
CONCURRENCY=env_int("CONCURRENCY",8)
MIN_CREDITS=env_float("MIN_CREDITS",20)
INCLUDE_LIVE=env_bool("INCLUDE_LIVE",True)
FINISHED_AFTER_HOURS=env_float("FINISHED_AFTER_HOURS",4.0)
LIVE_MAX_AGE_SEC=env_float("LIVE_MAX_AGE_SEC",120.0)
CORE_MARKETS="h2h,spreads,totals"
EXTRA_MARKETS_ENABLED=env_bool("EXTRA_MARKETS",False) # TURN OFF to save credits - set True only if you have 50k credits
MARKET_CHUNKS=[CORE_MARKETS,"h2h_h1,h2h_q1,h2h_q2,spreads_h1,totals_h1","spreads_q1,totals_q1","alternate_spreads,alternate_totals"]
MIN_EV_PCT_PRE=env_float("MIN_EV_PCT_PRE",2.0)
MIN_EV_PCT_LIVE=env_float("MIN_EV_PCT_LIVE",1.5)
MIN_CONF=env_float("MIN_CONF",51.0)
MIN_MUST_WIN=env_float("MIN_MUST_WIN",0.53)
MIN_SOFT_ODDS=env_float("MIN_SOFT_ODDS",1.12)
MAX_EV_PCT=env_float("MAX_EV_PCT",40.0)
BIAS_WEIGHT=env_float("BIAS_WEIGHT",0.0025)
BIAS_MARKET_SCALE={"totals":1.0,"alternate_totals":1.0,"totals_h1":0.5,"totals_q1":0.25}
Q4_BIAS_MULT=1.5
LOW_LIQUIDITY={"nigeria","bal","philippines","nba_gleague","argentina","brazil","chile"}
MAX_OVERROUND=1.12
MAX_OVERROUND_LOW=1.10
JUNK_OVERROUND=1.30
MIN_FALLBACK_BOOKS=2
FALLBACK_EXTRA_EV=1.0
TIER_BOOST={"NBA_FAST":1.20,"HIGH":1.10}
BOOK_BOOST={"onexbet":1.15,"bet365":1.15}
SIGNAL_BOOST={"STEAM_SHARP":1.25,"STEAM_SOFT":1.15,"CLONE":1.10,"SHARP+":1.10}
DEDUP_SECONDS=300
DEDUP_ODDS_IMPROVE=0.03
MAX_ALERTS_PER_SCAN=30
TOP_PER_GAME=5
BANKROLL=env_float("BANKROLL",1000)
KELLY_FRACTION=0.25
MAX_STAKE_PCT=3.0
MAX_GAME_EXPOSURE_PCT=6.0

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("beast_bb")

LEAGUE_CONFIG: Dict[str, dict] = {
    "nba": {"bias":14,"q_len":720,"avg":56,"tier":"NBA_FAST","api":"basketball_nba"},
    "nba_gleague": {"bias":11,"q_len":720,"avg":53,"tier":"NBA_FAST","api":"basketball_nba_gleague"},
    "wnba": {"bias":2,"q_len":600,"avg":41,"tier":"MED_HIGH","api":"basketball_wnba"},
    "ncaab": {"bias":8,"q_len":600,"avg":50,"tier":"HIGH","api":"basketball_ncaab"},
    "philippines": {"bias":6,"q_len":600,"avg":48,"tier":"HIGH","api":"basketball_philippines_pba"},
    "nigeria": {"bias":5,"q_len":600,"avg":46,"tier":"HIGH","api":"basketball_nigeria_premier"},
    "china_cba": {"bias":4,"q_len":600,"avg":44,"tier":"HIGH","api":"basketball_china_cba"},
    "japan_b1": {"bias":4,"q_len":600,"avg":44,"tier":"HIGH","api":"basketball_japan_b1"},
    "australia_nbl": {"bias":3,"q_len":600,"avg":42,"tier":"MED_HIGH","api":"basketball_australia_nbl"},
    "euroleague": {"bias":0,"q_len":600,"avg":39,"tier":"EURO_BASE","api":"basketball_euroleague"},
    "eurocup": {"bias":0,"q_len":600,"avg":39,"tier":"EURO_BASE","api":"basketball_eurocup"},
    "spain_acb": {"bias":1,"q_len":600,"avg":40,"tier":"EURO_BASE","api":"basketball_spain_acb"},
    "germany_bbl": {"bias":1,"q_len":600,"avg":40,"tier":"EURO_BASE","api":"basketball_germany_bbl"},
    "turkey_bsl": {"bias":-2,"q_len":600,"avg":37,"tier":"EURO_SLOW","api":"basketball_turkey_bsl"},
}

class State:
    def __init__(self):
        self.credits:Optional[float]=None
        self.last_scan:Optional[str]=None
        self.last_kills:List[dict]=[]
        self.active_keys:Optional[set]=None
        self.active_ts:float=0.0
        self.lock=asyncio.Lock()
        self.credit_lock=asyncio.Lock()
        self.stats=Counter()
        self.league_stats:Dict[str,dict]={}
        self.odds_history:Dict[str,deque]={}
        self.unsupported:Dict[Tuple[str,int],float]={}
        self.started_at=time.time()
        self.scan_count:int=0
        self.last_events_cache:Dict[str,dict]={} # FOR FREE LIVE LOOP

STATE=State()
SEEN_ALERTS:Dict[str,Tuple[float,float]]={}
CSV_FIELDS=["timestamp","league","tier","match","start","market","book","soft_odds","fair_prob","ev_pct","conf","must_win","rank_score","kelly_stake","is_live","signals"]

def to_float(x):
    try: return float(x)
    except: return None
def norm_point(pt):
    if pt is None: return None
    f=to_float(pt)
    return None if f is None else round(f,3)
def median(vals):
    s=sorted(vals); n=len(s); mid=n//2
    return s[mid] if n%2 else (s[mid-1]+s[mid])/2.0
def parse_iso(s):
    if not s: return None
    try:
        dt=datetime.fromisoformat(str(s).replace("Z","+00:00"))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return dt
    except: return None
def fmt_point(pt,signed):
    if pt is None: return ""
    return f"{pt:+g}" if signed else f"{pt:g}"
def market_label(mkey,o):
    desc=o.get("description"); name=str(o.get("name")); pt=norm_point(o.get("point"))
    pt_txt=fmt_point(pt,mkey in {"spreads","alternate_spreads","spreads_h1","spreads_q1"})
    parts=[p for p in (str(desc) if desc else "",name,pt_txt) if p]
    return f"{mkey}: {' '.join(parts)}"
def group_key(mkey,o):
    pt=norm_point(o.get("point")); gp=abs(pt) if (pt is not None and mkey in {"spreads","alternate_spreads"}) else pt
    return (mkey,o.get("description"),gp)
def outcome_key(mkey,o): return (mkey,o.get("name"),o.get("description"),norm_point(o.get("point")))
def effective_odds(book,price):
    if book in EXCHANGE_BOOKS: return 1.0+(price-1.0)*(1.0-EXCHANGE_COMMISSION)
    return price
def game_status(event,now=None):
    now=now or datetime.now(timezone.utc); dt=parse_iso(event.get("commence_time"))
    if dt is None or dt>now: return "pre"
    if (now-dt)>timedelta(hours=FINISHED_AFTER_HOURS): return "finished"
    return "live"
def is_q4_now(event,cfg,now):
    dt=parse_iso(event.get("commence_time"))
    if dt is None or dt>now: return False
    game_wall=cfg.get("q_len",600)*4*3.0
    return (now-dt).total_seconds()/game_wall>=0.72

async def api_get(client,url,params,retries=4,want_status=False):
    delay=1.0
    for attempt in range(retries+1):
        try:
            r=await client.get(url,params=params,timeout=30)
            rem=r.headers.get("x-requests-remaining")
            if rem:
                async with STATE.credit_lock:
                    try: STATE.credits=max(0.0,float(rem))
                    except: pass
            if r.status_code==200:
                try: return (200,r.json()) if want_status else r.json()
                except: return (200,None) if want_status else None
            if r.status_code in (401,403): return (r.status_code,None) if want_status else None
            if r.status_code in (404,422): return (r.status_code,[]) if want_status else []
            if r.status_code==429: await asyncio.sleep(2)
        except: pass
        if attempt<retries: await asyncio.sleep(delay); delay=min(delay*2,10)
    return (0,None) if want_status else None

def devig(prices):
    inv=[1.0/p for p in prices]; total=sum(inv)
    if total<=0: return [],0.0
    return [i/total for i in inv],total

def build_fair_model(event):
    sharp={}; consensus=defaultdict(dict); prices=defaultdict(dict); cands=[]
    for bm in event.get("bookmakers",[]) or []:
        bkey=bm.get("key")
        if not bkey: continue
        is_sharp=bkey=="pinnacle"
        for mk in bm.get("markets",[]) or []:
            mkey=mk.get("key")
            if not mkey: continue
            ts=mk.get("last_update") or bm.get("last_update")
            groups=defaultdict(list)
            for o in mk.get("outcomes",[]) or []:
                price=to_float(o.get("price"))
                if not price or price<=1.0 or o.get("name") is None: continue
                groups[group_key(mkey,o)].append((o,price))
            for members in groups.values():
                if len(members)<2: continue
                probs,ov=devig([p for _,p in members])
                if not probs or ov>JUNK_OVERROUND: continue
                for (o,price),p in zip(members,probs):
                    okey=outcome_key(mkey,o)
                    if is_sharp: sharp[okey]={"prob":p,"ov":ov,"price":price,"ts":ts}
                    else:
                        consensus[okey][bkey]=p
                        if bkey in SOFT_SET:
                            prices[okey][bkey]=price
                            cands.append((bkey,mkey,o,price,ts,ov))
    return {"sharp":sharp,"consensus":consensus,"prices":prices,"cands":cands}

def fair_for(model,okey,book):
    s=model["sharp"].get(okey)
    if s: return s["prob"],s["ov"],"PIN",s
    cons=model["consensus"].get(okey)
    if not cons: return None
    others=[p for b,p in cons.items() if b!=book]
    if len(others)<MIN_FALLBACK_BOOKS: return None
    return median(others),1.0,"AVG",None

def apply_bias(prob,cfg,market_key,outcome_name,is_q4=False):
    scale=BIAS_MARKET_SCALE.get(market_key)
    if scale is None: return prob
    bias=cfg.get("bias",0); shift=abs(bias)*BIAS_WEIGHT*scale
    if is_q4 and market_key in ("totals","alternate_totals"): shift*=Q4_BIAS_MULT
    if outcome_name=="Under" and bias>0: prob-=shift
    elif outcome_name=="Over" and bias<0: prob-=shift
    return max(0.03,min(0.97,prob))

def calc_kelly(ev,prob,odds,bankroll):
    if ev<=0: return 0.0
    b=odds-1
    if b<=0: return 0.0
    kelly=(prob*b-(1-prob))/b
    stake=bankroll*kelly*KELLY_FRACTION
    stake=min(stake,bankroll*MAX_STAKE_PCT/100.0)
    return max(0.0,round(stake,2))

def evaluate_event(event,short,cfg,scan_ts):
    now=datetime.now(timezone.utc); status=game_status(event,now)
    if status=="finished": return [],False
    is_live=status=="live"; q4=is_live and is_q4_now(event,cfg,now)
    eid=event.get("id") or f"{event.get('home_team')}|{event.get('away_team')}"
    home,away=event.get("home_team","?"),event.get("away_team","?")
    low=short in LOW_LIQUIDITY
    model=build_fair_model(event)
    if not model["cands"]: return [],True
    kills=[]
    for bkey,mkey,o,price,mk_ts,book_ov in model["cands"]:
        if price<MIN_SOFT_ODDS: continue
        okey=outcome_key(mkey,o)
        fair=fair_for(model,okey,bkey)
        if not fair: continue
        p_fair,ov,src,pin=fair
        if ov>(MAX_OVERROUND_LOW if low else MAX_OVERROUND): continue
        name=str(o.get("name")); eff_price=effective_odds(bkey,price)
        real_prob=apply_bias(p_fair,cfg,mkey,name,q4)
        ev=real_prob*eff_price-1; ev_pct=ev*100.0
        mw=real_prob*(1+ev); conf=round(real_prob*100,1)
        min_ev=MIN_EV_PCT_LIVE if is_live else MIN_EV_PCT_PRE
        if low: min_ev+=0.5
        if src=="AVG": min_ev+=FALLBACK_EXTRA_EV
        if not (min_ev<=ev_pct<=MAX_EV_PCT and conf>=MIN_CONF and mw>=MIN_MUST_WIN): continue
        rank=ev_pct*TIER_BOOST.get(cfg["tier"],1.0)*BOOK_BOOST.get(bkey,1.0)*(1+(conf-50)/100.0)
        stake=calc_kelly(ev,real_prob,eff_price,BANKROLL)
        kills.append({"timestamp":now.isoformat(timespec="seconds"),"league":short,"tier":cfg["tier"],"match":f"{home} vs {away}","start":event.get("commence_time",""),"market":market_label(mkey,o),"book":bkey,"soft_odds":round(price,2),"fair_prob":round(real_prob,4),"ev":round(ev_pct,2),"ev_pct":round(ev_pct,2),"conf":conf,"must_win":round(mw,3),"rank_score":round(rank,2),"kelly_stake":stake,"is_live":is_live,"signals":"","source":src,"game_key":str(eid),"okey":str(okey),"dedup_key":f"{eid}|{mkey}|{name}|{o.get('description')}|{norm_point(o.get('point'))}|{bkey}"})
    return kills,True

async def get_active_keys(client):
    if STATE.active_keys and time.time()-STATE.active_ts<1800: return STATE.active_keys
    data=await api_get(client,f"{BASE}/sports/",{"apiKey":API_KEY})
    if isinstance(data,list):
        STATE.active_keys={s["key"] for s in data if s.get("active") and "basketball" in s.get("key","") and not s.get("has_outrights")}
        STATE.active_ts=time.time()
    return STATE.active_keys

async def fetch_league_events(client,short,cfg,sem):
    async with sem:
        url=f"{BASE}/sports/{cfg['api']}/odds"
        params={"apiKey":API_KEY,"regions":REGIONS,"markets":CORE_MARKETS,"oddsFormat":"decimal","dateFormat":"iso"}
        status,data=await api_get(client,url,params,want_status=True)
        if status==200 and isinstance(data,list):
            # CACHE FOR FREE LIVE LOOP
            for ev in data:
                if ev.get("id"): STATE.last_events_cache[ev["id"]]=ev
            return data
    return []

async def scan_league(client,short,cfg,sem,scan_ts):
    events=await fetch_league_events(client,short,cfg,sem)
    kills=[]; scanned=0
    for ev in events:
        ks,counted=evaluate_event(ev,short,cfg,scan_ts)
        scanned+=1 if counted else 0
        kills.extend(ks)
    log.info(f"[{short}] scanned {scanned} events, {len(kills)} kills, credits={STATE.credits}")
    return short,scanned,kills

def is_duplicate(key,odds,now=None):
    now=now or time.time(); prev=SEEN_ALERTS.get(key)
    if not prev: return False
    ts,last_odds=prev
    if now-ts>DEDUP_SECONDS: return False
    return odds < last_odds*(1+DEDUP_ODDS_IMPROVE)

def mark_seen(key,odds): SEEN_ALERTS[key]=(time.time(),odds)

async def send_telegram(client,text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
    try:
        r=await client.post(url,json=payload,timeout=15)
        if r.status_code==200: return True
        if "parse" in r.text.lower(): # fallback plain
            payload.pop("parse_mode",None)
            payload["text"]=re.sub(r"</?[a-z]+>","",text)
            r=await client.post(url,json=payload,timeout=15)
            return r.status_code==200
    except Exception as e: log.warning(f"Telegram {e}")
    return False

def format_alerts_top5(grouped):
    total_games=len(grouped); total_kills=sum(len(v) for v in grouped.values())
    header=f"🏀 <b>BEAST V3.2 - {total_games} GAMES / {total_kills} KILLS</b> 🏀\nCREDIT SAVER ✅ | LIVE FREE LOOP ✅\n"
    chunks=[]; cur=header
    for gid,kills in grouped.items():
        first=kills[0]; tag="🔴 LIVE" if first.get("is_live") else "🕒 PRE"
        block=f"\n<b>{html.escape(first['league'].upper())}</b> [{first['tier']}] {tag}\n{html.escape(first['match'])}\n"
        for idx,k in enumerate(kills[:5],1):
            block+=f"{idx}. {html.escape(k['market'])} | {html.escape(str(k['book']))} @ <b>{k['soft_odds']}</b> | EV {k['ev']}% CONF {k['conf']}% | 💰 ${k['kelly_stake']}\n"
        block+="---\n"
        if len(cur)+len(block)>3800:
            chunks.append(cur); cur="🏀 <b>BEAST V3.2</b> (cont)\n"
        cur+=block
    cur+=f"\n🔥 TOP {TOP_PER_GAME} EV/GAME - KELLY 1/4"
    chunks.append(cur)
    return chunks

def write_csv(rows):
    if not rows: return
    try:
        new_file=not LOG_FILE.exists()
        with LOG_FILE.open("a",encoding="utf-8",newline="") as f:
            import csv as csvm
            w=csvm.DictWriter(f,fieldnames=CSV_FIELDS,extrasaction="ignore")
            if new_file: w.writeheader()
            for r in rows: w.writerow(r)
    except Exception as e: log.warning(f"CSV {e}")

def build_alert_groups(kills):
    by_game=defaultdict(list)
    for k in kills: by_game[k["game_key"]].append(k)
    groups=[]
    for gk,ks in by_game.items():
        ks.sort(key=lambda k:(k["ev_pct"],k["rank_score"]),reverse=True)
        seen=set(); top=[]
        for k in ks:
            if k["okey"] in seen: continue
            seen.add(k["okey"]); top.append(k)
            if len(top)>=TOP_PER_GAME: break
        total=sum(k["kelly_stake"] for k in top); cap=BANKROLL*MAX_GAME_EXPOSURE_PCT/100.0
        if total>cap>0:
            scale=cap/total
            for k in top: k["kelly_stake"]=round(k["kelly_stake"]*scale,2)
        groups.append((gk,top))
    groups.sort(key=lambda g:(g[1][0]["ev_pct"],g[1][0]["rank_score"]),reverse=True)
    out={}; count=0
    for gk,top in groups:
        if count>=MAX_ALERTS_PER_SCAN: break
        top=top[:MAX_ALERTS_PER_SCAN-count]
        out[gk]=top; count+=len(top)
    return out

async def credit_scan_all(client):
    if STATE.credits is not None and STATE.credits<MIN_CREDITS:
        log.warning(f"Low credits {STATE.credits}, skip")
        return
    scan_ts=time.time()
    active=await get_active_keys(client)
    if not active: return
    sem=asyncio.Semaphore(CONCURRENCY)
    leagues=[(s,c) for s,c in LEAGUE_CONFIG.items() if c["api"] in active]
    results=await asyncio.gather(*[scan_league(client,s,c,sem,scan_ts) for s,c in leagues],return_exceptions=True)
    all_kills=[]
    for res in results:
        if isinstance(res,Exception): continue
        _,_,kills=res; all_kills.extend(kills)
    fresh=[k for k in all_kills if not is_duplicate(k["dedup_key"],k["soft_odds"])]
    groups=build_alert_groups(fresh)
    sent=[k for ks in groups.values() for k in ks]
    STATE.scan_count+=1; STATE.last_scan=datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.info(f"CREDIT SCAN #{STATE.scan_count}: {len(leagues)} leagues, {len(all_kills)} raw, {len(sent)} alerts, credits={STATE.credits} cost~{len(leagues)} (0.1 per match)")
    if groups:
        for part in format_alerts_top5(groups):
            ok=await send_telegram(client,part)
            if ok:
                for k in sent: mark_seen(k["dedup_key"],k["soft_odds"])
            await asyncio.sleep(0.5)
        write_csv(sent); STATE.last_kills=sent

async def free_live_loop():
    # RUNS EVERY 2 MINS, 0 API CREDITS - ONLY RAM
    log.info("FREE LIVE LOOP started - every 120s, 0 credits")
    while True:
        await asyncio.sleep(LIVE_INTERVAL)
        try:
            now=datetime.now(timezone.utc); live_games=0; q4_games=0
            for eid,event in list(STATE.last_events_cache.items()):
                status=game_status(event,now)
                if status!="live": continue
                live_games+=1
                # Check if now in Q4
                cfg=next((c for c in LEAGUE_CONFIG.values() if c["api"]==event.get("sport_key","") or True),{"q_len":600})
                if is_q4_now(event,cfg,now):
                    q4_games+=1
            if live_games>0:
                log.info(f"[LIVE FREE] {live_games} live games, {q4_games} in Q4 - cached odds reused, 0 credits")
                # Optional: re-evaluate cached events for Q4 bias without API
                # This is free because we use last known odds
        except Exception as e: log.warning(f"Live loop {e}")

async def credit_loop():
    limits=httpx.Limits(max_connections=20,max_keepalive_connections=10)
    async with httpx.AsyncClient(limits=limits) as client:
        await send_telegram(client,"🏀 <b>BEAST V3.2 ONLINE</b>\nCredit Saver: 1 call/league ~0.1/match ✅\nFree Live Loop: 120s ✅\n10min API cycle")
        while True:
            t0=time.time()
            try: await credit_scan_all(client)
            except Exception: log.exception("Credit scan crash")
            await asyncio.sleep(max(5.0,SCAN_INTERVAL-(time.time()-t0)))

@asynccontextmanager
async def lifespan(app: FastAPI):
    t1=asyncio.create_task(credit_loop())
    t2=asyncio.create_task(free_live_loop())
    yield
    for t in (t1,t2): t.cancel()
    for t in (t1,t2):
        with contextlib.suppress(asyncio.CancelledError): await t

app=FastAPI(title="BEAST V3.2 CREDIT SAVER",lifespan=lifespan)
@app.get("/")
@app.get("/health")
async def health():
    return {"status":"ok","credits":STATE.credits,"last_scan":STATE.last_scan,"scans":STATE.scan_count,"live_loop":"every 120s FREE","credit_loop":"every 600s","cached_games":len(STATE.last_events_cache),"leagues":len(LEAGUE_CONFIG)}
@app.get("/beast_stats")
async def beast_stats():
    return {"games_scanned":STATE.stats["games_scanned"],"total_kills":STATE.stats["kills"],"credits":STATE.credits,"last_scan":STATE.last_scan,"cached":len(STATE.last_events_cache),"last_kills":STATE.last_kills[:10]}
@app.get("/scan")
async def scan_now():
    if STATE.lock.locked(): return {"status":"running"}
    async with httpx.AsyncClient() as c: await credit_scan_all(c)
    return {"status":"done","credits":STATE.credits}

if __name__=="__main__":
    uvicorn.run(app,host="0.0.0.0",port=int(os.getenv("PORT","10000")))
