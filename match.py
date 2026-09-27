"""
match.py - BEAST V4.2 ENGINE CORE
"""
from typing import List, Dict

ALL_MARKETS = ["1X2_HOME","1X2_DRAW","1X2_AWAY","OU_2.5_OVER","OU_2.5_UNDER","BTTS_YES","BTTS_NO","DC_1X","DC_X2","DC_12"]
LEAGUE_CONFIG = {"EPL": {"margin": 0.02, "sharp": True}, "LaLiga": {"margin": 0.025, "sharp": True}, "SerieA": {"margin": 0.03, "sharp": True}, "Bundesliga": {"margin": 0.03, "sharp": False}, "Ligue1": {"margin": 0.035, "sharp": False}}

def prob(odds): return 1/odds if odds>1 else 0
def devig_probs(siblings):
    if not siblings: return [0.5]
    inv = [1/o for o in siblings if o>1]; tot=sum(inv)
    return [i/tot for i in inv] if tot>0 else [0.5]

def multi_source_devig(sources):
    if not sources: return 0.5
    avg = sum([1/s["odds"] for s in sources if s.get("odds")])/len(sources)
    return avg

def calculate_beast(league, home, away, market_type, pin, soft, pin_open=None, siblings=None, multi=None, minute=None, red=None, sh=0, sa=0):
    if pin<=1 or soft<=1: return None
    true_p = prob(pin)
    config = LEAGUE_CONFIG.get(league, {"margin":0.03})
    # BEAST FORMULA
    ev = (soft / pin - 1) * 100
    conf = 0.95 if config["sharp"] else 0.75
    if ev<0: conf*=0.5
    kelly = max(0, (true_p*soft -1)/(soft-1))*100 if soft>1 else 0
    return {
        "fixture": f"{home} vs {away}", "league": league, "market": market_type,
        "pinnacle_odds": pin, "soft_odds": soft, "true_prob": round(true_p*100,2),
        "ev_percent": round(ev,2), "confidence": round(conf,2),
        "kelly_pct": round(kelly,2), "is_value": ev>=5, "is_beast": ev>=8 and conf>=0.7,
        "book_note": f"Sharp {league} | EV {ev:.1f}%"
    }

def get_top5_per_match(bets): return sorted(bets, key=lambda x: x["ev_percent"], reverse=True)[:5]
def scan_prematch(odds_list):
    res={}
    for o in odds_list:
        b=calculate_beast(o["league"],o["home"],o["away"],o["market_type"],o["pinnacle_odds"],o["soft_odds"],o.get("pinnacle_open"),o.get("sibling_pin_odds"),o.get("multi_sources"))
        if b and b["is_value"]:
            res.setdefault(b["fixture"], []).append(b)
    for f in res: res[f]=get_top5_per_match(res[f])
    return res
def scan_live(odds_list):
    res={}
    for o in odds_list:
        if o.get("live_red_card"): continue
        b=calculate_beast(o["league"],o["home"],o["away"],o["market_type"],o["pinnacle_odds"],o["soft_odds"],live_minute=o.get("live_minute"))
        if b and b["ev_percent"]>=3:
            res.setdefault(b["fixture"], []).append(b)
    return res
def scan_all_matches(odds_list): return scan_prematch(odds_list)

def calculate_clv(taken, close, siblings=None):
    if taken<=1 or close<=1: return None
    clv = (taken/close -1)*100
    return {"clv_percent": round(clv,2), "beat_close": taken>close, "is_sharp": clv>1.5}
