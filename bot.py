import asyncio
import json
import os
import re
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
import pandas as pd
from datetime import datetime, timezone, date
from anthropic import Anthropic
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, MessageHandler, CommandHandler,
                          CallbackQueryHandler, filters, ContextTypes)

import indicators as ind

# ═══════════════════════════════════════════════════════════════
#  CONFIG  (Railway Environment Variables)
# ═══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
TWELVE_KEY       = os.getenv("TWELVE_DATA_KEY", "")
AI_MODEL_SIGNAL  = os.getenv("AI_MODEL_SIGNAL", "claude-sonnet-4-6")  # Signal-Analyse (günstig, gute Qualität)
AI_MODEL_CHAT    = os.getenv("AI_MODEL_CHAT", "claude-sonnet-4-6")     # Chat-Fragen (günstig)
AI_MODEL_DEEP    = os.getenv("AI_MODEL_DEEP", "claude-opus-4-8")       # /deep Premium-Recherche

# Scalping: kostenlose technische Analyse (Standard). USE_AI_ANALYSIS=true schaltet
# wieder auf die kostenpflichtige KI-Analyse mit Websuche um.
USE_AI_ANALYSIS  = os.getenv("USE_AI_ANALYSIS", "false").lower() == "true"

# Da die Analyse kostenlos ist, etwas mehr liquide Paare (Daten-Limit Twelve Data bleibt der Engpass).
DEFAULT_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD",
    "EUR/JPY", "GBP/JPY", "XAU/USD",
]
_pairs_env = os.getenv("PAIRS", "").strip()
PAIRS = ([p.strip().upper() for p in _pairs_env.split(",") if p.strip()]
         if _pairs_env else DEFAULT_PAIRS)

STRATEGY          = os.getenv("STRATEGY", "orderblock").lower()  # "orderblock" | "trend" | "range" | "rsi2" | "technical"
_default_interval = "15min" if STRATEGY == "technical" else "1h"
INTERVAL          = os.getenv("INTERVAL", _default_interval)   # Zeitfenster der Analyse
HTF_INTERVAL      = os.getenv("HTF_INTERVAL", "1h" if STRATEGY == "technical" else "4h")  # übergeordneter Trend
PRESCREEN_MIN     = 55          # Technischer Vorab-Filter (nur "technical")
CONFIDENCE_MIN    = int(os.getenv("CONFIDENCE_MIN", "70"))  # Confidence-Schwelle
MAX_SIGNALS_DAY   = int(os.getenv("MAX_SIGNALS_DAY", "6"))
MAX_SIGNALS_PER_SCAN = 2        # nicht mehr als 2 Signale auf einmal
MAX_AI_PER_SCAN   = 2           # max. KI-Analysen pro Scan (Kostenschutz)
MIN_RR            = 1.5         # Mindest-Chance-Risiko-Verhältnis (orderblock/trend/technical)
SCAN_INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MIN", "25"))   # Scan-Takt
_default_cd       = {"orderblock": "6", "trend": "6", "range": "3", "rsi2": "2"}.get(STRATEGY, "2")
SIGNAL_COOLDOWN_H = int(os.getenv("SIGNAL_COOLDOWN_H", _default_cd))  # selbes Paar nicht öfter signalisieren
ANALYSIS_COOLDOWN_H = 2         # selbes Paar nicht öfter als alle 2h analysieren
TREND_ADX_MIN     = float(os.getenv("TREND_ADX_MIN", "23"))    # Mindest-Trendstärke (nur "trend")
RANGE_ADX_MAX     = float(os.getenv("RANGE_ADX_MAX", "20"))    # max. Trendstärke (nur "range": Seitwärtsmarkt)
RANGE_HTF_ADX_MAX = float(os.getenv("RANGE_HTF_ADX_MAX", "28"))# Veto: starker 4h-Trend = kein Fading
RANGE_MIN_RR      = float(os.getenv("RANGE_MIN_RR", "0.5"))    # Mindest-R:R fürs nahe Mean-Reversion-Ziel
# ── STRATEGY=rsi2 (Connors RSI-2 Mean Reversion) ──
RSI2_BUY          = float(os.getenv("RSI2_BUY", "10"))    # RSI(2) < x → Long-Erschöpfung
RSI2_SELL         = float(os.getenv("RSI2_SELL", "90"))   # RSI(2) > x → Short-Erschöpfung
RSI2_TREND_LEN    = int(os.getenv("RSI2_TREND_LEN", "0")) # 200=nur mit SMA-Trend, 0=Filter AUS (mehr Trades)
RSI2_STOP_ATR     = float(os.getenv("RSI2_STOP_ATR", "3.0"))  # Disaster-Stop in ATR (weit)
RSI2_MIN_RR       = float(os.getenv("RSI2_MIN_RR", "0.3"))    # Mindest-R:R Ziel(Bollinger-Mitte) vs. Stop
TRAIL_ENABLED     = os.getenv("TRAIL", "true").lower() in ("true", "1", "yes", "ja", "on")
TRAIL_ATR_MULT    = float(os.getenv("TRAIL_ATR_MULT", "2.0"))  # Trailing-Abstand in ATR
HTF_TOP_CANDIDATES = 3          # nur für die besten N Kandidaten den HTF-Trend laden (Credit-Schutz)

# Lot-Vorschlag im Signal: Kontogröße + Risiko-Band (skaliert mit Confidence)
ACCOUNT_SIZE  = float(os.getenv("ACCOUNT_SIZE", "100000"))   # dein (Demo-)Konto
RISK_MIN_PCT  = float(os.getenv("RISK_MIN_PCT", "0.25"))     # Risiko bei min. Confidence
RISK_MAX_PCT  = float(os.getenv("RISK_MAX_PCT", "1.0"))      # Risiko bei Confidence 100
MAX_LOT       = float(os.getenv("MAX_LOT", "5"))             # Obergrenze für den Vorschlag

# ─── OANDA-Trading (Demo standardmäßig) ───
OANDA_TOKEN      = os.getenv("OANDA_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV        = os.getenv("OANDA_ENV", "practice")        # practice = Demo, live = echt
TRADING_ENABLED  = os.getenv("TRADING_ENABLED", "false").lower() == "true"
# AUTO_TRADE=true → Signale werden automatisch ausgeführt (KEINE Accept-Bestätigung nötig)
AUTO_TRADE       = os.getenv("AUTO_TRADE", "false").lower() in ("true", "1", "yes", "ja", "on")
# Breakeven: sobald der Trade BREAKEVEN_AT_R im Gewinn ist, Stop auf Einstieg ziehen
BREAKEVEN_ENABLED = os.getenv("BREAKEVEN", "true").lower() in ("true", "1", "yes", "ja", "on")
BREAKEVEN_AT_R    = float(os.getenv("BREAKEVEN_AT_R", "1.0"))   # ab wie viel R Gewinn auf BE ziehen
OANDA_BASE = ("https://api-fxpractice.oanda.com" if OANDA_ENV == "practice"
              else "https://api-fxtrade.oanda.com")
NUM_TPS          = int(os.getenv("NUM_TPS", "3"))            # Anzahl Take-Profit-Stufen (= Teil-Trades)

# ─── Broker-Auswahl: "capital" (Capital.com) oder "oanda" ───
BROKER           = os.getenv("BROKER", "capital").lower()
# Capital.com (Demo standardmäßig)
CAPITAL_API_KEY    = os.getenv("CAPITAL_API_KEY", "")
CAPITAL_IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER", "")     # Login/E-Mail
CAPITAL_PASSWORD   = os.getenv("CAPITAL_PASSWORD", "")       # das CUSTOM-Passwort des API-Keys
CAPITAL_ENV        = os.getenv("CAPITAL_ENV", "demo")        # demo oder live
CAPITAL_SIZE_FACTOR = float(os.getenv("CAPITAL_SIZE_FACTOR", "1.0"))  # Lot -> Capital-Size kalibrieren
CAPITAL_MIN_SIZE    = float(os.getenv("CAPITAL_MIN_SIZE", "0"))       # manueller Mindestgrößen-Override
CAPITAL_BASE = ("https://demo-api-capital.backend-capital.com" if CAPITAL_ENV == "demo"
                else "https://api-capital.backend-capital.com")
TD_MIN_GAP        = 8.0         # min. Sekunden zwischen Twelve-Data-Calls (≈8/min)
# Speicherort: für dauerhafte Logs in Railway ein Volume mounten und STATE_DIR daraufsetzen
STATE_DIR         = os.getenv("STATE_DIR", ".")
STATE_FILE        = os.path.join(STATE_DIR, "state.json")
TRADELOG_FILE     = os.path.join(STATE_DIR, "trade_log.json")
DASHBOARD_PORT    = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8080")))

anthropic = Anthropic(api_key=ANTHROPIC_KEY)
scan_lock = asyncio.Lock()

# Durchsatz-Begrenzer für Twelve Data (Free: 8 Calls/Min). Thread-sicher,
# da fetch_* über asyncio.to_thread in Worker-Threads laufen.
_td_lock = threading.Lock()
_td_last = [0.0]

def _td_throttle():
    with _td_lock:
        now = _time.monotonic()
        wait = TD_MIN_GAP - (now - _td_last[0])
        if wait > 0:
            _time.sleep(wait)
        _td_last[0] = _time.monotonic()

# ═══════════════════════════════════════════════════════════════
#  HELFER — JSON, Session, Validierung
# ═══════════════════════════════════════════════════════════════
def extract_json(text: str) -> dict | None:
    """Robuster JSON-Extraktor mit echtem Klammer-Matching."""
    candidates = []
    # 1) Codeblöcke
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    # 2) Balanciertes Klammer-Matching für Objekte mit "trade"
    for st in (i for i, c in enumerate(text) if c == "{"):
        depth = 0
        for j in range(st, len(text)):
            if text[j] == "{": depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    blob = text[st:j+1]
                    if '"trade"' in blob:
                        candidates.append(blob)
                    break
    for blob in reversed(candidates):
        try:
            return json.loads(blob)
        except Exception:
            continue
    return None

def current_session() -> str:
    """Aktuelle Forex-Session (UTC). Überlappung London/NY = beste Liquidität."""
    h = datetime.now(timezone.utc).hour
    london = 7 <= h < 16
    newyork = 12 <= h < 21
    if london and newyork: return "London/NY-Überlappung 🔥 (beste Liquidität)"
    if london:  return "London"
    if newyork: return "New York"
    if 22 <= h or h < 7: return "Sydney/Tokio (dünner)"
    return "Übergang"

def is_prime_session() -> bool:
    h = datetime.now(timezone.utc).hour
    return 7 <= h < 21   # London + NY

def safe_float(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None

def validate_signal(s: dict) -> dict | None:
    """
    Prüft Level-Konsistenz und rechnet R:R aus den ECHTEN Levels nach
    (Opus' selbst gemeldetes risk_reward wird überschrieben).
    """
    entry = safe_float(s.get("entry"))
    sl    = safe_float(s.get("stop_loss"))
    tp    = safe_float(s.get("take_profit"))
    conf  = safe_float(s.get("confidence"))
    d     = s.get("direction")
    if None in (entry, sl, tp, conf) or d not in ("long", "short"):
        return None
    if d == "long" and not (sl < entry < tp):
        print(f"    Level-Logik ungültig (long): SL {sl} / E {entry} / TP {tp}")
        return None
    if d == "short" and not (tp < entry < sl):
        print(f"    Level-Logik ungültig (short): TP {tp} / E {entry} / SL {sl}")
        return None
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return None
    # Take-Profit-Liste normalisieren + validieren (jede Stufe auf der richtigen Seite)
    tps = s.get("take_profits") or [tp]
    clean_tps = []
    for t in tps:
        tf = safe_float(t)
        if tf is None:
            continue
        if d == "long" and tf > entry:
            clean_tps.append(tf)
        elif d == "short" and tf < entry:
            clean_tps.append(tf)
    if not clean_tps:
        return None
    clean_tps = sorted(clean_tps, reverse=(d == "short"))
    s["take_profits"] = clean_tps
    s["take_profit"] = clean_tps[0]
    s["entry"], s["stop_loss"] = entry, sl
    s["confidence"] = conf
    s["risk_reward"] = round(abs(clean_tps[0] - entry) / risk, 2)   # R:R der ersten Stufe
    return s

# ═══════════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════════
chat_history: list = []
signals_today = {"date": str(date.today()), "count": 0}
last_signal_time: dict = {}     # pair -> ISO timestamp (nach gesendetem Signal)
last_analysis_time: dict = {}   # pair -> ISO timestamp (nach Opus-Analyse, egal welches Ergebnis)
trade_log: list = []            # alle Signale + ausgeführte Trades + Ergebnisse (fürs Dashboard)
last_exec: dict = {}            # zuletzt ausgeführte Order-Infos (Size etc.) für die Protokollierung
scan_stats = {"last_run": None, "candidates": 0, "rejections": [],
              "signals_this_run": [], "ai_calls": 0, "summary": None, "closed_market": False}

def save_trade_log():
    try:
        with open(TRADELOG_FILE, "w") as f:
            json.dump(trade_log, f)
    except Exception as e:
        print(f"[TRADELOG save] {e}")

def load_trade_log():
    global trade_log
    if not os.path.exists(TRADELOG_FILE):
        return
    try:
        with open(TRADELOG_FILE) as f:
            trade_log = json.load(f)
    except Exception as e:
        print(f"[TRADELOG load] {e}")

def log_signal(pair: str, s: dict, strategy: str, df) -> str:
    """Protokolliert ein gesendetes Signal. Gibt die Datensatz-ID zurück."""
    rid = f"{pair.replace('/','')}-{int(datetime.now().timestamp())}-{len(trade_log)}"
    entry_dt = None
    try:
        if df is not None and "datetime" in df.columns and len(df):
            entry_dt = str(df["datetime"].iloc[-1])
    except Exception:
        pass
    trade_log.append({
        "id": rid,
        "suggested_at": datetime.now().isoformat(timespec="seconds"),
        "entry_dt": entry_dt,                 # letzte Kerze bei Signal (Feed-Zeit, für Outcome)
        "pair": pair, "strategy": strategy,
        "direction": s["direction"],
        "entry": round(s["entry"], 6),
        "stop_loss": round(s["stop_loss"], 6),
        "take_profits": [round(x, 6) for x in s["take_profits"]],
        "tp1": round(s["take_profits"][0], 6),
        "confidence": round(float(s["confidence"]), 1),
        "rr": round(float(s["risk_reward"]), 2),
        "haltedauer": s.get("haltedauer", ""),
        "opened": False,                       # vom Nutzer per Accept geöffnet?
        "opened_at": None,
        "order_type": None,                    # Markt/Limit
        "exec_size": None,                     # tatsächliche Capital-Size je Teil-Order
        "orders_ok": None,
        "status": "suggested",                # suggested | open | win | loss
        "result_r": None,                      # erreichtes R (TP1 = +rr, SL = -1)
        "closed_at": None,
    })
    save_trade_log()
    return rid

def mark_trade_opened(rid: str):
    """Markiert einen Datensatz als vom Nutzer geöffnet (mit echter Order-Größe)."""
    for r in trade_log:
        if r["id"] == rid:
            r["opened"] = True
            r["opened_at"] = datetime.now().isoformat(timespec="seconds")
            r["status"] = "open" if r["status"] == "suggested" else r["status"]
            r["order_type"] = last_exec.get("order_type")
            r["exec_size"] = last_exec.get("size")
            r["orders_ok"] = last_exec.get("orders_ok")
            r["capital_deals"] = last_exec.get("deal_map") or []   # [{id, tp}] für Stop-Management
            r["be_done"] = False                                    # Stop schon auf BE gezogen?
            r["cur_stop"] = r["stop_loss"]                          # aktueller Stop (für Trailing)
            save_trade_log()
            return

def update_open_outcomes(pair: str, df):
    """Trägt Ergebnisse (TP1 oder SL zuerst getroffen) für offene Datensätze nach — preisbasiert."""
    if df is None or "datetime" not in df.columns or not len(df):
        return
    changed = False
    for r in trade_log:
        if r["pair"] != pair or r["status"] not in ("suggested", "open"):
            continue
        # nur Kerzen NACH dem Signal betrachten (gleicher Feed → Zeitzonen passen)
        try:
            after = df
            if r.get("entry_dt"):
                after = df[df["datetime"] > r["entry_dt"]]
            if after is None or not len(after):
                continue
            highs = after["high"].to_numpy()
            lows = after["low"].to_numpy()
        except Exception:
            continue
        sl, tp1, direction = r["stop_loss"], r["tp1"], r["direction"]
        hit = None
        # chronologisch durchgehen: was kam zuerst?
        for hi, lo in zip(highs, lows):
            if direction == "long":
                sl_hit = lo <= sl
                tp_hit = hi >= tp1
            else:
                sl_hit = hi >= sl
                tp_hit = lo <= tp1
            if sl_hit and tp_hit:
                hit = "loss"; break          # beides in einer Kerze → konservativ als Verlust werten
            if sl_hit:
                hit = "loss"; break
            if tp_hit:
                hit = "win"; break
        if hit:
            r["status"] = hit
            r["result_r"] = round(r["rr"], 2) if hit == "win" else -1.0
            r["closed_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
    if changed:
        save_trade_log()

def manage_open_positions(market: dict) -> list:
    """
    Stop-Management für offene Capital-Trades:
    - Breakeven: ab BREAKEVEN_AT_R Gewinn Stop auf Einstieg
    - Trailing:  ab Gewinn den Stop in Trendrichtung nachziehen (TRAIL_ATR_MULT × ATR)
    Stop wird NIE verschlechtert, TP bleibt erhalten. market = {pair: {price, atr}}.
    """
    if BROKER != "capital":
        return []
    todo = [r for r in trade_log if r.get("status") == "open" and r.get("capital_deals")]
    if not todo:
        return []
    cst, xsec, err = capital_login()
    if err:
        return []
    msgs, changed = [], False
    for r in todo:
        m = market.get(r["pair"])
        if not m:
            continue
        price, atr = m.get("price"), m.get("atr", 0)
        entry, direction = r["entry"], r["direction"]
        risk = abs(entry - r["stop_loss"])
        if not price or risk <= 0:
            continue
        cur = r.get("cur_stop", r["stop_loss"])
        progress = (price - entry) if direction == "long" else (entry - price)
        target = cur

        # 1) Breakeven ab BREAKEVEN_AT_R
        if BREAKEVEN_ENABLED and progress >= BREAKEVEN_AT_R * risk - 1e-9:
            if direction == "long":
                target = max(target, entry)
            else:
                target = min(target, entry)
        # 2) Trailing ab Gewinn (mind. BREAKEVEN_AT_R), nie schlechter als bisher
        if TRAIL_ENABLED and atr > 0 and progress >= max(BREAKEVEN_AT_R, 1.0) * risk - 1e-9:
            if direction == "long":
                target = max(target, price - TRAIL_ATR_MULT * atr)
            else:
                target = min(target, price + TRAIL_ATR_MULT * atr)

        improved = (target > cur + 1e-9) if direction == "long" else (target < cur - 1e-9)
        if not improved:
            continue

        digits = capital_digits(r["pair"])
        results = []
        for deal in r["capital_deals"]:
            did, tp = (deal.get("id"), deal.get("tp")) if isinstance(deal, dict) else (deal, None)
            if not did:
                continue
            results.append(capital_update_stop(cst, xsec, did, target, tp, digits))
        if any(results):
            r["cur_stop"] = round(target, 6)
            changed = True
            rr_done = round(progress / risk, 2)
            crossed_be = (target >= entry - 1e-9) if direction == "long" else (target <= entry + 1e-9)
            if crossed_be and not r.get("be_done"):
                r["be_done"] = True
                msgs.append(f"🔒 {r['pair']} {direction.upper()}: Stop auf Breakeven ({target:.5f}) — "
                            f"+{rr_done}R, kann nicht mehr ins Minus drehen.")
            else:
                msgs.append(f"📈 {r['pair']} {direction.upper()}: Stop nachgezogen auf {target:.5f} "
                            f"(+{rr_done}R gesichert, Gewinner läuft weiter).")
    if changed:
        save_trade_log()
    return msgs

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"signals_today": signals_today,
                       "last_signal_time": last_signal_time,
                       "last_analysis_time": last_analysis_time}, f)
    except Exception as e:
        print(f"[STATE save] {e}")

# ═══════════════════════════════════════════════════════════════
#  BACKTEST (STRATEGY=range) — simuliert die Live-Logik auf Historie
# ═══════════════════════════════════════════════════════════════
BT_SPREAD_PIPS = {   # konservative Spread-Annahmen (Pips) — Schätzwerte!
    "EUR/USD": 0.8, "GBP/USD": 1.0, "USD/JPY": 0.9, "USD/CHF": 1.2,
    "AUD/USD": 0.9, "USD/CAD": 1.3, "NZD/USD": 1.4,
    "EUR/JPY": 1.6, "GBP/JPY": 2.5, "XAU/USD": 4.0,
}

def backtest_range_pair(pair: str, df, df4) -> list:
    """
    Spielt die range-Strategie Kerze für Kerze auf 1h-Historie durch.
    Identische Regeln wie build_range_signal + Stufe-2-Veto + Confidence-Gate + Cooldown.
    Outcome wie update_open_outcomes (SL und TP in derselben Kerze = konservativ Verlust).
    """
    if df is None or len(df) < 240 or df4 is None or len(df4) < 60:
        return []
    closes = df["close"].astype(float)
    c = closes.to_numpy(); h = df["high"].astype(float).to_numpy(); l = df["low"].astype(float).to_numpy()
    t = pd.to_datetime(df["datetime"])
    rsi_s = ind.rsi(closes).to_numpy()
    atr_s = ind.atr(df).to_numpy()
    adx_s = ind.adx(df).to_numpy()
    bb_up_s, bb_mid_s, bb_lo_s = ind.bollinger(closes)
    bb_up = bb_up_s.to_numpy(); bb_mid = bb_mid_s.to_numpy(); bb_lo = bb_lo_s.to_numpy()
    # 4h-ADX je 1h-Kerze: nur ABGESCHLOSSENE 4h-Kerzen (kein Blick in die Zukunft)
    t4_closed = pd.to_datetime(df4["datetime"]) + pd.Timedelta(hours=4)
    adx4 = ind.adx(df4).to_numpy()
    sp = BT_SPREAD_PIPS.get(pair, 1.5) * pip_size(pair)

    trades, last_sig = [], None
    for i in range(210, len(df) - 1):
        atr_i, adx_i, rsi_i = atr_s[i], adx_s[i], rsi_s[i]
        if pd.isna(atr_i) or pd.isna(adx_i) or atr_i <= 0:
            continue
        if adx_i >= RANGE_ADX_MAX:
            continue
        if last_sig is not None and (t.iloc[i] - last_sig).total_seconds() / 3600 < SIGNAL_COOLDOWN_H:
            continue
        k = int(t4_closed.searchsorted(t.iloc[i], side="right")) - 1   # letzte fertige 4h-Kerze
        a4 = float(adx4[k]) if (k >= 0 and not pd.isna(adx4[k])) else None
        if a4 is not None and a4 >= RANGE_HTF_ADX_MAX:
            continue
        price, prev = c[i], c[i - 1]
        touched_low  = bool((l[i-2:i+1] <= bb_lo[i]).any())
        touched_high = bool((h[i-2:i+1] >= bb_up[i]).any())
        direction = None
        if touched_low and rsi_i <= 34 and price > prev and price < bb_mid[i]:
            direction = "long"
        elif touched_high and rsi_i >= 66 and price < prev and price > bb_mid[i]:
            direction = "short"
        if direction is None:
            continue
        if direction == "long":
            sl, tp = float(l[i-3:i+1].min()) - 0.3 * atr_i, bb_mid[i]
            risk, reward = price - sl, tp - price
        else:
            sl, tp = float(h[i-3:i+1].max()) + 0.3 * atr_i, bb_mid[i]
            risk, reward = sl - price, price - tp
        if risk <= 0 or reward <= 0:
            continue
        rr = reward / risk
        if rr < RANGE_MIN_RR:
            continue
        depth = (34 - rsi_i) if direction == "long" else (rsi_i - 66)
        conf = 64
        conf += 12 if depth >= 5 else (7 if depth >= 2 else 3)
        conf += 8 if adx_i <= 16 else (5 if adx_i <= 18 else 2)
        if a4 is not None and a4 <= 20: conf += 6
        if rr >= 0.8:                   conf += 4
        if conf < CONFIDENCE_MIN:
            continue
        # Outcome: erste Folgekerze, die SL oder TP trifft (beides zugleich -> Verlust)
        hit = None
        for j in range(i + 1, len(df)):
            sl_hit = (l[j] <= sl) if direction == "long" else (h[j] >= sl)
            tp_hit = (h[j] >= tp) if direction == "long" else (l[j] <= tp)
            if sl_hit:
                hit = "loss"; break
            if tp_hit:
                hit = "win"; break
        # Spread-Abzug: Einstieg um den Spread verschlechtert -> kleineres effektives R:R
        e_eff = price + sp if direction == "long" else price - sp
        risk_eff = (e_eff - sl) if direction == "long" else (sl - e_eff)
        rew_eff  = (tp - e_eff) if direction == "long" else (e_eff - tp)
        rr_eff = (rew_eff / risk_eff) if risk_eff > 0 else 0.0
        trades.append({
            "pair": pair, "time": t.iloc[i], "direction": direction, "conf": conf,
            "rr": rr, "rr_eff": rr_eff, "result": hit,
            "r":     (round(rr, 3)     if hit == "win" else (-1.0 if hit == "loss" else None)),
            "r_eff": (round(rr_eff, 3) if hit == "win" else (-1.0 if hit == "loss" else None)),
        })
        last_sig = t.iloc[i]
    return trades

def _bt_aggregate(trades: list, key: str) -> dict:
    closed = [x for x in trades if x["result"] in ("win", "loss")]
    if not closed:
        return {"n": 0}
    wins  = [x for x in closed if x["result"] == "win"]
    total = sum(x[key] for x in closed)
    pos   = sum(x[key] for x in closed if x[key] > 0)
    neg   = sum(x[key] for x in closed if x[key] < 0)
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in sorted(closed, key=lambda z: z["time"]):
        cum += x[key]; peak = max(peak, cum); dd = min(dd, cum - peak)
    return {"n": len(closed), "w": len(wins), "l": len(closed) - len(wins),
            "wr": len(wins) / len(closed), "total": total, "exp": total / len(closed),
            "pf": (pos / abs(neg)) if neg else float("inf"), "maxdd": dd,
            "avg_rr": (sum(x["rr"] for x in wins) / len(wins)) if wins else 0.0}

def run_backtest(days: int = 60) -> str:
    """Backtest der range-Strategie über alle Paare. Läuft synchron (im Thread aufrufen)."""
    size1 = min(5000, int(days * 24 * 5 / 7) + 280)
    size4 = min(5000, int(days * 6 * 5 / 7) + 280)
    all_trades, lines, failed = [], [], []
    for pair in PAIRS:
        df  = fetch_ohlc(pair, interval="1h", size=size1)
        df4 = fetch_ohlc(pair, interval="4h", size=size4)
        if df is None or df4 is None or len(df) < 240:
            failed.append(pair)
            continue
        all_trades.extend(backtest_range_pair(pair, df, df4))
    if not all_trades:
        extra = f" (keine Daten: {', '.join(failed)})" if failed else ""
        return ("🧪 Backtest range: Im Zeitraum gab es KEIN einziges Signal, das alle Filter "
                "bestanden hat" + extra + ".\nDas ist ein ehrliches Ergebnis: Die Filter sind streng. "
                "Lockern ginge (RANGE_ADX_MAX höher), macht die Trefferquote aber tendenziell schlechter.")
    # Tageslimit global anwenden (max MAX_SIGNALS_DAY/Tag, chronologisch)
    all_trades.sort(key=lambda x: x["time"])
    per_day, kept = {}, []
    for x in all_trades:
        d = x["time"].date()
        if per_day.get(d, 0) >= MAX_SIGNALS_DAY:
            continue
        per_day[d] = per_day.get(d, 0) + 1
        kept.append(x)
    open_n = sum(1 for x in kept if x["result"] is None)
    t0 = min(x["time"] for x in kept).date(); t1 = max(x["time"] for x in kept).date()
    g_raw, g_eff = _bt_aggregate(kept, "r"), _bt_aggregate(kept, "r_eff")
    out = [f"🧪 BACKTEST range — {t0} bis {t1} (1h, {len(PAIRS)} Paare)",
           "─────────────────────────"]
    for pair in PAIRS:
        pt = [x for x in kept if x["pair"] == pair]
        s = _bt_aggregate(pt, "r_eff")
        if s["n"] == 0:
            out.append(f"{pair}: keine abgeschlossenen Trades")
        else:
            out.append(f"{pair}: {s['n']} Trades, {s['w']}W/{s['l']}L = {s['wr']:.0%}, "
                       f"{s['total']:+.2f}R (m. Spread)")
    out.append("─────────────────────────")
    be_wr = 1 / (1 + g_raw["avg_rr"]) if g_raw.get("avg_rr") else None
    out.append(f"GESAMT: {g_raw['n']} abgeschlossen ({g_raw['w']}W/{g_raw['l']}L)"
               + (f" + {open_n} offen" if open_n else ""))
    out.append(f"Trefferquote: {g_raw['wr']:.1%}"
               + (f" | nötig (Break-even bei Ø-Ziel {g_raw['avg_rr']:.2f}R): {be_wr:.1%}" if be_wr else ""))
    out.append(f"Gesamt-R     roh: {g_raw['total']:+.2f}R | mit Spread: {g_eff['total']:+.2f}R")
    out.append(f"Erwartung/Tr roh: {g_raw['exp']:+.3f}R | mit Spread: {g_eff['exp']:+.3f}R")
    pf_raw = "∞" if g_raw["pf"] == float("inf") else f"{g_raw['pf']:.2f}"
    pf_eff = "∞" if g_eff["pf"] == float("inf") else f"{g_eff['pf']:.2f}"
    out.append(f"Profit-Faktor roh: {pf_raw} | mit Spread: {pf_eff} | Max. Drawdown: {g_eff['maxdd']:.2f}R")
    if failed:
        out.append(f"(ohne Daten: {', '.join(failed)})")
    out.append("")
    out.append("⚠️ Ehrlich: Das ist IN-SAMPLE auf einem Marktregime (2 Monate), Spreads sind "
               "Schätzwerte, Scan-Timing ist angenähert. Ein guter Backtest ist eine "
               "Voraussetzung, KEIN Beweis — die Demo-Phase bleibt der Schiedsrichter.")
    return "\n".join(out)

def load_state():
    global signals_today, last_signal_time, last_analysis_time
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        signals_today = d.get("signals_today", signals_today)
        last_signal_time = d.get("last_signal_time", {})
        last_analysis_time = d.get("last_analysis_time", {})
    except Exception as e:
        print(f"[STATE load] {e}")

def reset_daily_if_needed():
    today = str(date.today())
    if signals_today["date"] != today:
        signals_today["date"] = today
        signals_today["count"] = 0
        save_state()

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════
async def tg_send(bot: Bot, text: str):
    if not TELEGRAM_CHAT_ID:
        return
    try:
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[i:i+4000])
    except Exception as e:
        print(f"[TG ERROR] {e}")

# ═══════════════════════════════════════════════════════════════
#  TWELVE DATA — OHLC laden
# ═══════════════════════════════════════════════════════════════
def fetch_ohlc(pair: str, interval: str = INTERVAL, size: int = 250) -> pd.DataFrame | None:
    _td_throttle()
    url = ("https://api.twelvedata.com/time_series"
           f"?symbol={pair}&interval={interval}&outputsize={size}&apikey={TWELVE_KEY}")
    try:
        r = requests.get(url, timeout=20)
        data = r.json()
        if data.get("status") != "ok" or "values" not in data:
            print(f"[TwelveData] {pair}: {data.get('message', 'keine Daten')}")
            return None
        rows = data["values"][::-1]   # chronologisch (alt -> neu)
        df = pd.DataFrame(rows)
        for c in ("open", "high", "low", "close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna().reset_index(drop=True)
    except Exception as e:
        print(f"[TwelveData ERROR] {pair}: {e}")
        return None

def fetch_price(pair: str) -> float | None:
    _td_throttle()
    url = f"https://api.twelvedata.com/price?symbol={pair}&apikey={TWELVE_KEY}"
    try:
        r = requests.get(url, timeout=15)
        return safe_float(r.json().get("price"))
    except Exception:
        return None

def htf_trend(pair: str) -> dict | None:
    """Übergeordneter Trend für Multi-Timeframe-Confluence."""
    df = fetch_ohlc(pair, interval=HTF_INTERVAL, size=250)
    if df is None or len(df) < 210:
        return None
    return ind.analyze(df)

# ═══════════════════════════════════════════════════════════════
#  PIP-HELFER
# ═══════════════════════════════════════════════════════════════
def pip_size(pair: str) -> float:
    if pair.startswith("XAU"): return 0.1     # Gold (broker-abhängig)
    if pair.startswith("XAG"): return 0.01    # Silber
    if "JPY" in pair: return 0.01
    return 0.0001

def pips_between(pair: str, a: float, b: float) -> float:
    return abs(a - b) / pip_size(pair)

def pip_value_per_lot(pair: str, price: float) -> float:
    """Pip-Wert pro Standard-Lot (100k) grob in Kontowährung (≈USD/EUR)."""
    if pair.endswith("USD"):
        return 10.0
    if pair.startswith("USD") and price > 0:
        return (pip_size(pair) / price) * 100000
    if pair.startswith(("XAU", "XAG")):
        return 10.0   # grobe Näherung (broker-abhängig)
    return 10.0

def suggest_lot(confidence: float, sl_pips: float, pair: str, price: float):
    """
    Lot-Vorschlag skaliert mit Confidence:
    niedrige Confidence → kleine Lot, hohe → größere (innerhalb Risiko-Band).
    Gibt (lots, risk_pct, capped) zurück.
    """
    span = max(1.0, 100 - CONFIDENCE_MIN)
    frac = min(max((confidence - CONFIDENCE_MIN) / span, 0.0), 1.0)
    risk_pct = RISK_MIN_PCT + frac * (RISK_MAX_PCT - RISK_MIN_PCT)
    risk_amount = ACCOUNT_SIZE * risk_pct / 100
    pvl = pip_value_per_lot(pair, price)
    lots = risk_amount / (sl_pips * pvl) if (sl_pips > 0 and pvl > 0) else 0
    capped = False
    if lots > MAX_LOT:
        lots, capped = MAX_LOT, True
    lots = max(round(lots, 2), 0.01)
    return lots, risk_pct, capped

# ═══════════════════════════════════════════════════════════════
#  KI-ANALYSE  (Opus 4.8 mit Live-Websuche)
# ═══════════════════════════════════════════════════════════════
def build_technical_signal(pair: str, direction: str, a: dict, htf: dict | None) -> dict | None:
    """
    Kostenlose, deterministische Signal-Erzeugung aus den Indikatoren.
    Entry = Pullback, Stop = ATR-basiert (volatilitätsadaptiv, kein fester %),
    Take Profit = nächstes Swing-Level oder 2R, Confidence aus Confluence.
    Gibt dasselbe Dict-Format wie die KI zurück (für validate_signal/format_signal).
    """
    price = a["price"]
    atr   = a["atr"]
    if atr <= 0 or price <= 0:
        return None

    risk = 1.5 * atr
    if direction == "long":
        entry = price - 0.3 * atr           # leichter Pullback-Einstieg
        sl    = entry - risk
        base  = entry - risk
        tp1 = entry + 1.5 * risk
        tp2 = entry + 2.5 * risk
        tp3 = a["swing_high"] if a["swing_high"] > entry + 1.5 * risk else entry + 4.0 * risk
        tps = sorted([tp1, tp2, tp3])[:NUM_TPS]
    else:  # short
        entry = price + 0.3 * atr
        sl    = entry + risk
        tp1 = entry - 1.5 * risk
        tp2 = entry - 2.5 * risk
        tp3 = a["swing_low"] if a["swing_low"] < entry - 1.5 * risk else entry - 4.0 * risk
        tps = sorted([tp1, tp2, tp3], reverse=True)[:NUM_TPS]

    tp_first = tps[0]
    rr = abs(tp_first - entry) / abs(entry - sl) if entry != sl else 0
    if round(rr, 2) < MIN_RR:
        return {"trade": False}

    # ── Confidence aus Confluence (0-100) ──
    adx, rsi = a["adx"], a["rsi"]
    hist, hist_prev = a["macd_hist"], a["macd_hist_prev"]
    conf = 50
    conf += 15 if adx >= 30 else 10 if adx >= 22 else 5            # Trendstärke
    if direction == "long":
        conf += 10 if 45 <= rsi <= 65 else 5 if 40 <= rsi < 70 else 0
        if hist > 0 and hist > hist_prev: conf += 10               # Momentum dreht hoch
    else:
        conf += 10 if 35 <= rsi <= 55 else 5 if 30 < rsi <= 60 else 0
        if hist < 0 and hist < hist_prev: conf += 10
    aligned = htf and ((direction == "long" and htf["trend"] == "bullish")
                       or (direction == "short" and htf["trend"] == "bearish"))
    if aligned: conf += 15                                          # Multi-Timeframe-Bestätigung
    if rr >= 2.5: conf += 5
    conf = max(0, min(conf, 100))

    # ── Haltedauer grob schätzen (bis TP1) ──
    ratio = abs(tp_first - entry) / atr
    mins = ratio * 15 * 1.2
    if mins < 60:     hold = f"~{max(15, int(round(mins/15))*15)} Min"
    elif mins < 180:  hold = "1-3 Stunden"
    else:             hold = "mehrere Stunden"

    trend_word = "Aufwärts" if direction == "long" else "Abwärts"
    reasoning = (f"{trend_word}trend (EMA20/50/200 gestaffelt), ADX {adx:.0f} = "
                 f"{'starker' if adx >= 25 else 'moderater'} Trend, RSI {rsi:.0f}, "
                 f"MACD-Momentum {'positiv' if hist > 0 else 'negativ'}"
                 f"{', 4h bestätigt' if aligned else ''}. "
                 f"Einstieg am Pullback, Stop 1,5×ATR (volatilitätsbasiert), "
                 f"{len(tps)} Take-Profit-Stufen zum Ausskalieren.")

    return {
        "trade": True,
        "direction": direction,
        "entry": entry, "stop_loss": sl,
        "take_profit": tp_first, "take_profits": tps,
        "confidence": conf, "risk_reward": round(rr, 2),
        "haltedauer": hold,
        "fundamental": "⚠️ Rein technisches Signal — keine News-/Fundamentalanalyse. "
                       "Bitte Wirtschaftskalender selbst prüfen (z.B. vor Zinsentscheid/NFP). "
                       "Für tiefe Recherche: /deep",
        "sentiment": "Technisch: Trend + Momentum bestätigt (siehe Begründung).",
        "reasoning": reasoning,
    }


def build_orderblock_signal(pair: str, df, a: dict, htf: dict | None) -> dict | None:
    """
    Signal nach Order-Block-/Break-of-Structure-Methode (Smart-Money-Stil).
    Einstieg im Retest der Order-Block-Zone, STRUKTURELLER Stop hinter dem Block
    (breiter als ein ATR-Scalp-Stop → weniger Noise-Ausstoppen), Ziele an der Liquidität.
    Gibt dasselbe Dict-Format wie build_technical_signal zurück.
    """
    atr = a["atr"]
    if atr <= 0:
        return {"trade": False}
    ob = ind.detect_order_block(df, atr)
    if not ob:
        return {"trade": False}

    direction = ob["direction"]
    entry = ob["price"]
    buf = 0.2 * atr                       # kleiner Puffer hinter den Block

    if direction == "long":
        sl = ob["ob_low"] - buf           # Stop UNTER dem Order-Block (strukturell)
        risk = entry - sl
        if risk <= 0:
            return {"trade": False}
        tp1 = ob["broke_level"] if ob["broke_level"] > entry + 1.0 * risk else entry + 1.5 * risk
        tp2 = entry + 2.5 * risk
        tp3 = entry + 4.0 * risk
        tps = sorted([tp1, tp2, tp3])[:NUM_TPS]
    else:  # short
        sl = ob["ob_high"] + buf          # Stop ÜBER dem Order-Block
        risk = sl - entry
        if risk <= 0:
            return {"trade": False}
        tp1 = ob["broke_level"] if ob["broke_level"] < entry - 1.0 * risk else entry - 1.5 * risk
        tp2 = entry - 2.5 * risk
        tp3 = entry - 4.0 * risk
        tps = sorted([tp1, tp2, tp3], reverse=True)[:NUM_TPS]

    tp_first = tps[0]
    rr = abs(tp_first - entry) / abs(entry - sl) if entry != sl else 0
    if round(rr, 2) < MIN_RR:
        return {"trade": False}

    # ── Confidence ──
    adx = a["adx"]
    aligned = htf and ((direction == "long" and htf["trend"] == "bullish")
                       or (direction == "short" and htf["trend"] == "bearish"))
    conf = 60                              # Basis: valider OB + BOS + Retest
    if aligned:        conf += 20          # in Richtung des übergeordneten Trends
    if adx >= 22:      conf += 10          # Struktur mit Trendstärke
    if rr >= 2.5:      conf += 5
    conf = max(0, min(conf, 100))

    hold = "mehrere Stunden bis 1-2 Tage"  # 1h/4h-Struktur-Trade
    trend_word = "Aufwärts" if direction == "long" else "Abwärts"
    zone = f"{ob['ob_low']:.5f}–{ob['ob_high']:.5f}"
    reasoning = (f"Order-Block-Setup: Struktur nach {('oben' if direction=='long' else 'unten')} "
                 f"gebrochen (BOS über/unter {ob['broke_level']:.5f}), Rückkehr in die "
                 f"Order-Block-Zone {zone}. Einstieg im Retest, struktureller Stop hinter dem Block "
                 f"({'unter' if direction=='long' else 'über'} der Zone, Puffer 0,2×ATR), "
                 f"Ziele an der Liquidität (gebrochenes Level + Erweiterungen)."
                 f"{' Übergeordneter ' + HTF_INTERVAL + '-Trend bestätigt.' if aligned else ' (Ohne HTF-Bestätigung — vorsichtiger.)'}")

    return {
        "trade": True,
        "direction": direction,
        "entry": entry, "stop_loss": sl,
        "take_profit": tp_first, "take_profits": tps,
        "confidence": conf, "risk_reward": round(rr, 2),
        "haltedauer": hold,
        "fundamental": "⚠️ Rein technisches Order-Block-Signal — keine News-/Fundamentalanalyse. "
                       "Wirtschaftskalender selbst prüfen. Für tiefe Recherche: /deep",
        "sentiment": f"Smart-Money-Struktur: {trend_word}-Bruch + Order-Block-Retest.",
        "reasoning": reasoning,
    }


def build_trend_signal(pair: str, df, a: dict, htf: dict | None) -> dict | None:
    """
    Trendfolge / Momentum-Pullback:
    - nur in Richtung eines starken Trends (EMA-Stapel + ADX), bestätigt vom HTF
    - Einstieg auf Pullback in Richtung Trend (Preis nahe EMA20), Momentum dreht zurück
    - struktureller Stop hinter Swing/EMA50; Gewinner laufen via Trailing-Stop (siehe manage_open_positions)
    Gibt dasselbe Dict-Format wie die anderen Signalbauer zurück.
    """
    atr, price = a["atr"], a["price"]
    if atr <= 0:
        return {"trade": False}
    ema20, ema50, ema200 = a["ema20"], a["ema50"], a["ema200"]
    adx, rsi = a["adx"], a["rsi"]

    up = price > ema50 > ema200
    down = price < ema50 < ema200
    if not (up or down) or adx < TREND_ADX_MIN:
        return {"trade": False}                       # nur klarer, starker Trend
    direction = "long" if up else "short"

    aligned = htf and ((direction == "long" and htf["trend"] == "bullish")
                       or (direction == "short" and htf["trend"] == "bearish"))
    if htf and not aligned:
        return {"trade": False}                       # kein Handel gegen den HTF-Trend

    if abs(price - ema20) > 1.0 * atr:                # Pullback: Preis nahe EMA20, nicht überdehnt
        return {"trade": False}
    # Momentum dreht in Trendrichtung zurück (MACD-Histogramm ODER letzter Schlusskurs)
    prev_close = float(df["close"].iloc[-2]) if len(df) >= 2 else price
    mom_up   = (a["macd_hist"] > a["macd_hist_prev"]) or (price > prev_close)
    mom_down = (a["macd_hist"] < a["macd_hist_prev"]) or (price < prev_close)
    if direction == "long" and not (rsi > 45 and mom_up):
        return {"trade": False}
    if direction == "short" and not (rsi < 55 and mom_down):
        return {"trade": False}

    entry = price
    if direction == "long":
        sl = min(a["swing_low"], ema50) - 0.2 * atr   # struktureller Stop unter Swing-Tief/EMA50
        risk = entry - sl
        if risk <= 0:
            return {"trade": False}
        tps = [entry + 2.0 * risk, entry + 3.5 * risk, entry + 5.0 * risk][:NUM_TPS]
    else:
        sl = max(a["swing_high"], ema50) + 0.2 * atr
        risk = sl - entry
        if risk <= 0:
            return {"trade": False}
        tps = [entry - 2.0 * risk, entry - 3.5 * risk, entry - 5.0 * risk][:NUM_TPS]

    rr = abs(tps[0] - entry) / risk
    if round(rr, 2) < MIN_RR:
        return {"trade": False}

    conf = 60
    if aligned:                    conf += 20
    if adx >= 28:                  conf += 10
    if abs(price - ema20) <= 0.3 * atr:  conf += 10      # sauberer, naher Pullback
    conf = max(0, min(conf, 100))

    word = "Aufwärts" if direction == "long" else "Abwärts"
    reasoning = (f"Trendfolge: klarer {word}-Trend (Preis {'>' if direction=='long' else '<'} EMA50 "
                 f"{'>' if direction=='long' else '<'} EMA200, ADX {adx:.0f}). Einstieg auf Pullback "
                 f"nahe EMA20 ({ema20:.5f}), Momentum dreht in Trendrichtung zurück. Struktureller Stop "
                 f"hinter Swing/EMA50; Gewinner laufen über Trailing-Stop ({TRAIL_ATR_MULT}×ATR)."
                 f"{' HTF (' + HTF_INTERVAL + ') bestätigt den Trend.' if aligned else ''}")

    return {
        "trade": True,
        "direction": direction,
        "entry": entry, "stop_loss": sl,
        "take_profit": tps[0], "take_profits": tps,
        "confidence": conf, "risk_reward": round(rr, 2),
        "haltedauer": "Stunden bis Tage (Trendfolge mit Trailing)",
        "fundamental": "⚠️ Rein technisches Trendfolge-Signal — keine News-/Fundamentalanalyse. "
                       "Wirtschaftskalender selbst prüfen. Für tiefe Recherche: /deep",
        "sentiment": f"Momentum/Trendfolge: mit dem {word}-Trend, Einstieg im Pullback.",
        "reasoning": reasoning,
    }


def build_range_signal(pair: str, df, a: dict, htf: dict | None) -> dict | None:
    """
    Mean Reversion / Range ("Hohe-Trefferquote-Design"):
    - NUR im Seitwärtsmarkt (ADX < RANGE_ADX_MAX), Veto bei starkem 4h-Trend
    - Erschöpfung am Bollinger-Rand (Band-Touch + RSI-Extrem) + Umkehrkerze
    - Ziel = NUR die Mitte der Range (Bollinger-Mittellinie) → nahes Ziel, hohe Trefferchance
    - dafür bewusst kleines R:R (~0.5-1.2R): viele kleine Gewinne, klassischer Trade-off
    """
    atr, price = a["atr"], a["price"]
    if atr <= 0 or len(df) < 25:
        return {"trade": False}
    adx, rsi = a["adx"], a["rsi"]
    if adx >= RANGE_ADX_MAX:
        return {"trade": False}                       # nur echter Seitwärtsmarkt
    if htf and htf.get("adx", 0) >= RANGE_HTF_ADX_MAX:
        return {"trade": False}                       # starker HTF-Trend → Ausbruchsgefahr, kein Fading

    bb_lo, bb_up = a["bb_lower"], a["bb_upper"]
    bb_mid = a.get("bb_mid") or (bb_lo + bb_up) / 2
    lows, highs, closes = df["low"].astype(float), df["high"].astype(float), df["close"].astype(float)
    prev_close = float(closes.iloc[-2])
    touched_low  = bool((lows.iloc[-3:]  <= bb_lo).any())   # Band-Touch in den letzten 3 Kerzen
    touched_high = bool((highs.iloc[-3:] >= bb_up).any())

    direction = None
    if touched_low and rsi <= 34 and price > prev_close and price < bb_mid:
        direction = "long"                            # überverkauft am Unterrand, Kerze dreht hoch
    elif touched_high and rsi >= 66 and price < prev_close and price > bb_mid:
        direction = "short"                           # überkauft am Oberrand, Kerze dreht runter
    if direction is None:
        return {"trade": False}

    if direction == "long":
        sl = float(lows.iloc[-4:].min()) - 0.3 * atr  # hinter dem lokalen Extrem
        tp = bb_mid                                   # EIN Ziel: zurück zur Mitte
        risk, reward = price - sl, tp - price
    else:
        sl = float(highs.iloc[-4:].max()) + 0.3 * atr
        tp = bb_mid
        risk, reward = sl - price, price - tp
    if risk <= 0 or reward <= 0:
        return {"trade": False}
    rr = reward / risk
    if rr < RANGE_MIN_RR:
        return {"trade": False}                       # Ziel zu nah → Spread frisst den Gewinn

    depth = (34 - rsi) if direction == "long" else (rsi - 66)
    conf = 64
    conf += 12 if depth >= 5 else (7 if depth >= 2 else 3)      # wie extrem ist der RSI
    conf += 8 if adx <= 16 else (5 if adx <= 18 else 2)         # wie klar ist die Range
    if htf and htf.get("adx", 99) <= 20:  conf += 6             # auch 4h ruhig
    if rr >= 0.8:                          conf += 4
    conf = min(conf, 100)

    word = "Unterrand" if direction == "long" else "Oberrand"
    reasoning = (f"Mean Reversion: Seitwärtsmarkt (ADX {adx:.0f} < {RANGE_ADX_MAX:.0f}), Erschöpfung am "
                 f"Bollinger-{word} (RSI {rsi:.0f}), Umkehrkerze. Ziel ist NUR die Range-Mitte "
                 f"({bb_mid:.5f}) — nahes Ziel = hohe Trefferchance, dafür kleines R:R ({rr:.2f}). "
                 f"Stop hinter dem lokalen Extrem."
                 f"{' 4h ruhig (ADX ' + format(htf['adx'], '.0f') + ') — kein Trend, der dagegenläuft.' if htf else ''}")

    return {
        "trade": True,
        "direction": direction,
        "entry": price, "stop_loss": sl,
        "take_profit": tp, "take_profits": [tp],
        "confidence": conf, "risk_reward": round(rr, 2),
        "haltedauer": "wenige Stunden (Rückkehr zur Range-Mitte)",
        "fundamental": "⚠️ Rein technisches Mean-Reversion-Signal — keine News-/Fundamentalanalyse. "
                       "Wirtschaftskalender selbst prüfen (News sprengen Ranges!). Für tiefe Recherche: /deep",
        "sentiment": f"Range-Markt: Erschöpfung am {word}, Rückkehr zur Mitte erwartet.",
        "reasoning": reasoning,
    }


def build_rsi2_signal(pair: str, df, a: dict, htf: dict | None) -> dict | None:
    """
    Connors RSI-2 Mean Reversion (STRATEGY=rsi2) — die in TradingView getestete Logik,
    angepasst an die feste Order-Infrastruktur des Bots.
    - Einstieg: RSI(2) < RSI2_BUY (Long) bzw. > RSI2_SELL (Short) — am Schluss des Extrem-Bars
    - optionaler Trendfilter: nur Long über SMA(RSI2_TREND_LEN), nur Short darunter (0 = aus)
    - Ziel: Bollinger-Mitte (statt dynamischem RSI-Ausstieg — der Bot kann nicht aktiv schließen)
    - Disaster-Stop: RSI2_STOP_ATR × ATR hinter dem Einstieg
    ⚠️ Ehrlich: Im Backtest über 1,5 Jahre war diese Strategie netto negativ (PF ~0,73).
       Das hier ist die Demo-Umsetzung zum Live-Beobachten, KEIN bewährter Edge.
    """
    atr, price = a["atr"], a["price"]
    if atr <= 0 or len(df) < 25:
        return {"trade": False}
    rsi2 = a.get("rsi2")
    if rsi2 is None:
        return {"trade": False}

    # optionaler SMA-Trendfilter (Standard aus)
    up_ok = down_ok = True
    if RSI2_TREND_LEN > 0:
        sma = a.get("sma200")
        if sma:
            up_ok, down_ok = price > sma, price < sma

    direction = None
    if rsi2 < RSI2_BUY and up_ok:
        direction = "long"
    elif rsi2 > RSI2_SELL and down_ok:
        direction = "short"
    if direction is None:
        return {"trade": False}

    bb_lo, bb_up = a["bb_lower"], a["bb_upper"]
    bb_mid = a.get("bb_mid") or (bb_lo + bb_up) / 2
    lows, highs = df["low"].astype(float), df["high"].astype(float)

    if direction == "long":
        sl = price - RSI2_STOP_ATR * atr
        tp = max(bb_mid, price + 0.5 * atr)           # Ziel mind. etwas über Einstieg
        risk, reward = price - sl, tp - price
    else:
        sl = price + RSI2_STOP_ATR * atr
        tp = min(bb_mid, price - 0.5 * atr)
        risk, reward = sl - price, price - tp
    if risk <= 0 or reward <= 0:
        return {"trade": False}
    rr = reward / risk
    if rr < RSI2_MIN_RR:
        return {"trade": False}

    depth = (RSI2_BUY - rsi2) if direction == "long" else (rsi2 - RSI2_SELL)
    conf = 66
    conf += 12 if depth >= 7 else (7 if depth >= 3 else 3)
    if RSI2_TREND_LEN > 0:  conf += 6                  # mit Trendfilter = höhere Qualität
    if rr >= 0.8:           conf += 4
    conf = min(conf, 100)

    word = "überverkauft" if direction == "long" else "überkauft"
    tf = f"über SMA{RSI2_TREND_LEN}" if (direction == "long" and RSI2_TREND_LEN > 0) else \
         (f"unter SMA{RSI2_TREND_LEN}" if RSI2_TREND_LEN > 0 else "Trendfilter aus")
    reasoning = (f"Connors RSI-2: RSI(2)={rsi2:.0f} ({word}), {tf}. Ziel = Bollinger-Mitte "
                 f"({bb_mid:.5f}), Disaster-Stop {RSI2_STOP_ATR:.0f}×ATR. R:R {rr:.2f}. "
                 f"⚠️ Demo-Test einer im Backtest netto negativen Strategie — beobachten, nicht vertrauen.")

    return {
        "trade": True,
        "direction": direction,
        "entry": price, "stop_loss": sl,
        "take_profit": tp, "take_profits": [tp],
        "confidence": conf, "risk_reward": round(rr, 2),
        "haltedauer": "Stunden bis wenige Tage (Rückkehr zur Mitte)",
        "fundamental": "⚠️ Rein technisch (RSI-2). Keine News-Analyse. Demo-Test, kein bewährter Edge. /deep für Recherche.",
        "sentiment": f"RSI(2) {word} — Rückkehr zur Mitte erwartet.",
        "reasoning": reasoning,
    }



    ps = pip_size(pair)
    htf_block = f"Keine {HTF_INTERVAL}-Daten verfügbar."
    if htf:
        htf_block = (f"{HTF_INTERVAL}-Trend: {htf['trend']} (EMA20={htf['ema20']:.5f}, "
                     f"EMA50={htf['ema50']:.5f}), RSI={htf['rsi']:.1f}, "
                     f"ADX={htf['adx']:.1f}, Swing-Hoch={htf['swing_high']:.5f}, "
                     f"Swing-Tief={htf['swing_low']:.5f}")
    prompt = f"""Du bist ein professioneller Forex-Daytrader. Analysiere {pair} für einen möglichen {('LONG' if direction=='long' else 'SHORT')}-Trade auf dem {INTERVAL}-Chart (Daytrading, Intraday).

TECHNISCHE DATEN {INTERVAL} (bereits berechnet):
- Aktueller Preis: {a['price']:.5f}
- Trend: {a['trend']} (EMA20={a['ema20']:.5f}, EMA50={a['ema50']:.5f}, EMA200={a['ema200']:.5f})
- RSI(14): {a['rsi']:.1f}
- MACD-Histogramm: {a['macd_hist']:.6f} (vorher {a['macd_hist_prev']:.6f})
- ADX: {a['adx']:.1f} (Trendstärke)
- ATR(14): {a['atr']:.5f}  ← nutze das für die Stop-Distanz (Volatilität)
- Jüngstes Swing-Hoch: {a['swing_high']:.5f}
- Jüngstes Swing-Tief: {a['swing_low']:.5f}
- Pip-Größe: {ps}

ÜBERGEORDNETER {HTF_INTERVAL}-KONTEXT (Multi-Timeframe):
- {htf_block}
- WICHTIG: Handle bevorzugt MIT dem {HTF_INTERVAL}-Trend. Wenn {INTERVAL} und {HTF_INTERVAL} widersprechen, sei besonders streng oder lehne ab.

DEINE AUFGABE — recherchiere im Web (nutze die Suche aktiv):
1. FUNDAMENTAL: Aktuelle Zinsdifferenz/Notenbank-Haltung der beiden Währungen, jüngste Wirtschaftsdaten, anstehende High-Impact-News in den nächsten 12h (ForexFactory-Kalender). Wenn ein großes Event unmittelbar bevorsteht → Confidence senken oder ablehnen.
2. SENTIMENT: Wie sind die erfolgreichen/großen Marktteilnehmer positioniert? Suche nach COT-Daten (Commitment of Traders), Retail-Sentiment (z.B. IG/Myfxbook), aktuelle Analystenpositionierung.

DANN entscheide:
- Lohnt sich der Trade in Richtung {direction.upper()} wirklich? Sei streng — nur hohe Sicherheit.
- WICHTIG — Scalping-Modus: Ziel-Haltedauer wenige Minuten bis wenige Stunden (Intraday, kein Übernacht-Halten). Lege Entry, Stop und Take Profit eng und realistisch für eine schnelle Bewegung — primär auf Basis der Technik (15min-Struktur). Recherchiere nur kurz, ob in den nächsten Stunden ein High-Impact-Event ansteht, das man meiden sollte.
- Bestimme Entry (Buy/Sell Limit auf sinnvollem Pullback-Level, nicht einfach Marktpreis).
- Bestimme Stop Loss aus echter Struktur: unter/über dem Swing-Level bzw. ca. 1.5×ATR vom Entry — KEIN fester Prozentsatz. Begründe.
- Bestimme Take Profit am nächsten relevanten Widerstand/Unterstützung (Swing-Level), passend zur Haltedauer. Das Chance-Risiko-Verhältnis MUSS mindestens {MIN_RR} sein, sonst ablehnen.
- Schätze die voraussichtliche Haltedauer (z.B. "~30 Min", "1-2 Stunden", "wenige Stunden").
- Vergib eine Confidence 0-100. Nur >= {CONFIDENCE_MIN} wird gehandelt.

Bei einem LONG muss gelten: Stop Loss < Entry < Take Profit.
Bei einem SHORT muss gelten: Take Profit < Entry < Stop Loss.

Antworte mit einer kurzen Analyse und am ENDE einem JSON-Block in genau diesem Format (nichts danach):
```json
{{"trade": true/false, "direction": "long/short", "entry": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "confidence": 0-100, "risk_reward": 0.0, "haltedauer": "1-2 Stunden", "fundamental": "1-2 Sätze", "sentiment": "1-2 Sätze", "reasoning": "warum dieser Entry/SL/TP - 2-3 Sätze"}}
```"""

    try:
        resp = anthropic.messages.create(
            model=AI_MODEL_SIGNAL,
            max_tokens=1200,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        signal = extract_json(text)
        if not signal:
            print(f"[AI] {pair}: kein JSON gefunden")
            return None
        signal["_analysis_text"] = text
        return signal
    except Exception as e:
        print(f"[AI ERROR] {pair}: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
#  SIGNAL-FORMAT
# ═══════════════════════════════════════════════════════════════
def format_signal(pair: str, s: dict, htf: dict | None) -> str:
    ps = pip_size(pair)
    entry, sl, tp = s["entry"], s["stop_loss"], s["take_profit"]
    sl_pips = pips_between(pair, entry, sl)
    tp_pips = pips_between(pair, entry, tp)
    is_long = s["direction"] == "long"
    arrow = "🟢 KAUFEN (Buy Limit)" if is_long else "🔴 VERKAUFEN (Sell Limit)"
    digits = 3 if ("JPY" in pair or pair.startswith("XAU")) else 5
    htf_line = f"\n📐 {HTF_INTERVAL}-Trend: {htf['trend']}" if htf else ""

    # Take-Profit-Zeilen (mehrere Stufen)
    tps = s.get("take_profits") or [s["take_profit"]]
    tp_lines = ""
    for i, t in enumerate(tps, 1):
        tp_lines += f"✅ TP{i}: {t:.{digits}f}  ({pips_between(pair, entry, t):.0f} Pips)\n"

    # Confidence-skalierter Lot-Vorschlag (gesamt, wird auf die TPs aufgeteilt)
    lots, risk_pct, capped = suggest_lot(s["confidence"], sl_pips, pair, entry)
    cap_note = " (gedeckelt)" if capped else ""

    return (
        f"📊 FOREX SIGNAL — {pair}\n"
        f"══════════════════════\n"
        f"{arrow}\n"
        f"🎯 Entry: {entry:.{digits}f}\n"
        f"🛑 Stop Loss: {sl:.{digits}f}  ({sl_pips:.0f} Pips)\n"
        f"{tp_lines}"
        f"⚖️ Chance/Risiko (TP1): 1:{s['risk_reward']:.1f}\n"
        f"⏳ Erwartete Haltedauer: {s.get('haltedauer', 'wenige Stunden')}\n"
        f"🎰 Confidence: {s['confidence']:.0f}/100"
        f"{htf_line}\n"
        f"🕐 Session: {current_session()}\n"
        f"\n💰 LOT-VORSCHLAG: {lots:.2f} Lots{cap_note}\n"
        f"   (Confidence {s['confidence']:.0f} → {risk_pct:.2f}% Risiko von {ACCOUNT_SIZE:,.0f})\n"
        f"   aufgeteilt auf {len(tps)} Teil-Trades (je {lots/len(tps):.2f})\n"
        f"\n💡 BEGRÜNDUNG\n{s.get('reasoning','-')}\n"
        f"{s.get('fundamental','')}\n"
        f"══════════════════════\n"
        f"⚠️ Kein Finanzrat. Prüfe selbst & nutze Risikomanagement."
    )

# ═══════════════════════════════════════════════════════════════
#  SCAN
# ═══════════════════════════════════════════════════════════════
def market_open() -> bool:
    """Forex: So ~22 UTC bis Fr ~22 UTC. Wochenende geschlossen."""
    now = datetime.now(timezone.utc)
    wd = now.weekday()  # Mo=0 ... So=6
    if wd == 5:  # Samstag
        return False
    if wd == 6 and now.hour < 22:  # Sonntag vor Open
        return False
    if wd == 4 and now.hour >= 22:  # Freitag nach Close
        return False
    return True

def cooldown_ok(pair: str) -> bool:
    ts = last_signal_time.get(pair)
    if not ts:
        return True
    last = datetime.fromisoformat(ts)
    return (datetime.now() - last).total_seconds() / 3600 >= SIGNAL_COOLDOWN_H

def analysis_cooldown_ok(pair: str) -> bool:
    """Verhindert, dass dasselbe Paar zu oft (teuer) von Opus analysiert wird."""
    ts = last_analysis_time.get(pair)
    if not ts:
        return True
    last = datetime.fromisoformat(ts)
    return (datetime.now() - last).total_seconds() / 3600 >= ANALYSIS_COOLDOWN_H

async def scan(bot: Bot):
    if scan_lock.locked():
        print(f"[{datetime.now():%H:%M}] Scan läuft bereits — übersprungen.")
        return
    async with scan_lock:
        reset_daily_if_needed()
        scan_stats["last_run"] = datetime.now().isoformat()
        scan_stats["candidates"] = 0
        scan_stats["rejections"] = []
        scan_stats["signals_this_run"] = []
        scan_stats["closed_market"] = False

        if signals_today["count"] >= MAX_SIGNALS_DAY:
            print(f"[{datetime.now():%H:%M}] Tageslimit ({MAX_SIGNALS_DAY}) erreicht.")
            scan_stats["summary"] = f"🔍 Scan: Tageslimit ({MAX_SIGNALS_DAY} Signale) erreicht. Morgen geht's weiter."
            return
        if not market_open():
            print(f"[{datetime.now():%H:%M}] Markt geschlossen.")
            scan_stats["closed_market"] = True
            scan_stats["summary"] = "🔍 Markt geschlossen (Wochenende) — keine Analyse."
            return

        print(f"\n[{datetime.now():%H:%M}] 🔍 Scan {len(PAIRS)} Paare ({STRATEGY}) | Session: {current_session()}")
        # ── Stufe 1: Kandidaten finden ({INTERVAL}) ──
        candidates = []
        market = {}                                # {pair: {price, atr}} fürs Stop-Management
        for pair in PAIRS:
            # df auch holen, wenn das Paar im Cooldown ist, ABER offene Trades hat (Outcome/Trailing)
            has_open = any(r["pair"] == pair and r["status"] in ("suggested", "open")
                           for r in trade_log)
            cooled = cooldown_ok(pair)
            if not cooled and not has_open:
                continue
            df = await asyncio.to_thread(fetch_ohlc, pair)
            if df is None or len(df) < 210:
                continue
            a = ind.analyze(df)
            market[pair] = {"price": float(df["close"].iloc[-1]), "atr": a["atr"]}
            update_open_outcomes(pair, df)        # Ergebnisse (TP1/SL) automatisch nachtragen
            if not cooled:
                continue                          # nur Outcome-/Stop-Update, kein neues Signal (Cooldown)
            if STRATEGY == "orderblock":
                ob = ind.detect_order_block(df, a["atr"])
                if ob:
                    direction = ob["direction"]
                    print(f"  {pair}: Order-Block {direction} erkannt (ADX {a['adx']:.0f}, Zone-Retest)")
                    candidates.append((pair, direction, a, int(a["adx"]), df))
                else:
                    print(f"  {pair}: kein Order-Block-Setup")
            elif STRATEGY == "trend":
                up = a["price"] > a["ema50"] > a["ema200"]
                down = a["price"] < a["ema50"] < a["ema200"]
                near = a["atr"] > 0 and abs(a["price"] - a["ema20"]) <= 1.0 * a["atr"]
                if (up or down) and a["adx"] >= TREND_ADX_MIN and near:
                    direction = "long" if up else "short"
                    print(f"  {pair}: Trend-Pullback {direction} (ADX {a['adx']:.0f}, nahe EMA20)")
                    candidates.append((pair, direction, a, int(a["adx"]), df))
                else:
                    print(f"  {pair}: kein Trend-Setup (ADX {a['adx']:.0f})")
            elif STRATEGY == "range":
                touched = bool((df["low"].astype(float).iloc[-3:] <= a["bb_lower"]).any() or
                               (df["high"].astype(float).iloc[-3:] >= a["bb_upper"]).any())
                extreme = a["rsi"] <= 36 or a["rsi"] >= 64
                if a["adx"] < RANGE_ADX_MAX and touched and extreme:
                    direction = "long" if a["rsi"] <= 36 else "short"
                    print(f"  {pair}: Range-Erschöpfung {direction} (ADX {a['adx']:.0f}, RSI {a['rsi']:.0f}, Band-Touch)")
                    candidates.append((pair, direction, a, int(abs(a["rsi"] - 50)), df))
                else:
                    print(f"  {pair}: kein Range-Setup (ADX {a['adx']:.0f}, RSI {a['rsi']:.0f})")
            elif STRATEGY == "rsi2":
                r2 = a.get("rsi2")
                up_ok = down_ok = True
                if RSI2_TREND_LEN > 0 and a.get("sma200"):
                    up_ok, down_ok = a["price"] > a["sma200"], a["price"] < a["sma200"]
                if r2 is not None and ((r2 < RSI2_BUY and up_ok) or (r2 > RSI2_SELL and down_ok)):
                    direction = "long" if r2 < RSI2_BUY else "short"
                    print(f"  {pair}: RSI-2 Extrem {direction} (RSI2 {r2:.0f}, Filter {'an' if RSI2_TREND_LEN else 'aus'})")
                    candidates.append((pair, direction, a, int(abs(r2 - 50)), df))
                else:
                    print(f"  {pair}: kein RSI-2-Setup (RSI2 {r2:.0f})" if r2 is not None else f"  {pair}: RSI2 n/a")
            else:
                score, direction = ind.prescreen_score(a)
                print(f"  {pair}: {INTERVAL}-prescreen {score}/100 ({direction}, ADX {a['adx']:.0f})")
                if score >= PRESCREEN_MIN and direction != "none":
                    candidates.append((pair, direction, a, score, df))

        # ── Stop-Management: Breakeven + Trailing für offene Trades ──
        for m in await asyncio.to_thread(manage_open_positions, market):
            await tg_send(bot, m)

        # Nur die besten Kandidaten weiterverfolgen (HTF-Fetch = Credits sparen)
        candidates.sort(key=lambda x: x[3], reverse=True)
        candidates = candidates[:HTF_TOP_CANDIDATES]

        # ── Stufe 2: {HTF_INTERVAL}-Multi-Timeframe-Filter (nur Top-Kandidaten) ──
        aligned = []
        for pair, direction, a, score, df in candidates:
            htf = None if STRATEGY == "rsi2" else await asyncio.to_thread(htf_trend, pair)
            if htf:
                if STRATEGY == "rsi2":
                    pass                              # rsi2: Trendfilter (falls an) sitzt am Einstieg, kein 4h-Veto
                elif STRATEGY == "range":
                    # Mean Reversion: kein Fading gegen einen STARKEN 4h-Trend (Ausbruchsgefahr)
                    if htf.get("adx", 0) >= RANGE_HTF_ADX_MAX:
                        print(f"  {pair}: verworfen — starker {HTF_INTERVAL}-Trend (ADX {htf['adx']:.0f}) gegen Range-Fading")
                        scan_stats["rejections"].append(f"{pair}: {HTF_INTERVAL}-Trend zu stark")
                        continue
                else:
                    # Konflikt mit höherem Zeitfenster → raus
                    if direction == "long" and htf["trend"] == "bearish":
                        print(f"  {pair}: verworfen — {direction} vs. {HTF_INTERVAL} bearish")
                        scan_stats["rejections"].append(f"{pair}: {HTF_INTERVAL}-Konflikt")
                        continue
                    if direction == "short" and htf["trend"] == "bullish":
                        print(f"  {pair}: verworfen — {direction} vs. {HTF_INTERVAL} bullish")
                        scan_stats["rejections"].append(f"{pair}: {HTF_INTERVAL}-Konflikt")
                        continue
            aligned.append((pair, direction, a, score, df, htf))

        scan_stats["candidates"] = len(aligned)

        # ── Stufe 3: Signal-Erzeugung (beste zuerst) ──
        aligned.sort(key=lambda x: x[3], reverse=True)
        ai_calls = 0
        sent_this_scan = 0
        for pair, direction, a, score, df, htf in aligned:
            if signals_today["count"] >= MAX_SIGNALS_DAY:
                break
            if sent_this_scan >= MAX_SIGNALS_PER_SCAN:
                print(f"  Max {MAX_SIGNALS_PER_SCAN} Signale/Scan erreicht — Rest beim nächsten Scan.")
                break

            if STRATEGY == "orderblock":
                # Order-Block-Methode (kostenlos, strukturbasiert)
                print(f"  → Order-Block-Analyse {pair} ({direction})...")
                raw = build_orderblock_signal(pair, df, a, htf)
            elif STRATEGY == "trend":
                # Trendfolge / Momentum-Pullback (kostenlos)
                print(f"  → Trendfolge-Analyse {pair} ({direction})...")
                raw = build_trend_signal(pair, df, a, htf)
            elif STRATEGY == "range":
                # Mean Reversion / Range (kostenlos, Hohe-Trefferquote-Design)
                print(f"  → Range-Analyse {pair} ({direction})...")
                raw = build_range_signal(pair, df, a, htf)
            elif STRATEGY == "rsi2":
                # Connors RSI-2 (Demo-Test, kostenlos)
                print(f"  → RSI-2-Analyse {pair} ({direction})...")
                raw = build_rsi2_signal(pair, df, a, htf)
            elif USE_AI_ANALYSIS:
                # Optionaler KI-Modus (kostenpflichtig, nur 'technical')
                if ai_calls >= MAX_AI_PER_SCAN:
                    print(f"  KI-Limit ({MAX_AI_PER_SCAN}) erreicht.")
                    break
                if not analysis_cooldown_ok(pair):
                    print(f"  {pair}: Analyse-Cooldown aktiv — übersprungen")
                    continue
                print(f"  → KI-Analyse {pair} ({direction})...")
                ai_calls += 1
                last_analysis_time[pair] = datetime.now().isoformat()
                save_state()
                raw = await asyncio.to_thread(deep_analysis, pair, direction, a, htf)
            else:
                # Kostenlose technische Engine
                print(f"  → Technische Analyse {pair} ({direction})...")
                raw = build_technical_signal(pair, direction, a, htf)

            if not raw or not raw.get("trade"):
                scan_stats["rejections"].append(f"{pair}: kein Setup")
                print(f"    abgelehnt (kein Trade)")
                continue
            s = validate_signal(raw)
            if not s:
                scan_stats["rejections"].append(f"{pair}: Level ungültig")
                print(f"    abgelehnt (Level-Validierung)")
                continue
            if s["confidence"] < CONFIDENCE_MIN:
                scan_stats["rejections"].append(f"{pair}: Confidence {s['confidence']:.0f}")
                print(f"    abgelehnt (Confidence {s['confidence']:.0f})")
                continue
            eff_min_rr = {"rsi2": RSI2_MIN_RR, "range": RANGE_MIN_RR}.get(STRATEGY, MIN_RR)
            if s["risk_reward"] < eff_min_rr:
                scan_stats["rejections"].append(f"{pair}: R:R {s['risk_reward']}")
                print(f"    abgelehnt (R:R {s['risk_reward']} < {eff_min_rr})")
                continue
            # ── Signal protokollieren (fürs Dashboard) + senden ──
            rid = log_signal(pair, s, STRATEGY, df)
            await send_signal_with_buttons(bot, pair, s, htf, rid)
            signals_today["count"] += 1
            sent_this_scan += 1
            last_signal_time[pair] = datetime.now().isoformat()
            scan_stats["signals_this_run"].append(f"{pair} {direction.upper()}")
            save_state()
            print(f"    ✅ SIGNAL gesendet ({s['confidence']:.0f}/100, R:R {s['risk_reward']})")

        # ── Scan-Zusammenfassung bauen (für Sofort-Meldung) ──
        scan_stats["ai_calls"] = ai_calls
        sigs = scan_stats["signals_this_run"]
        rejs = scan_stats["rejections"]
        t = datetime.now().strftime("%H:%M")
        if sigs:
            scan_stats["summary"] = (f"🔍 Scan {t}: {len(sigs)} Signal(e) gesendet "
                                     f"({', '.join(sigs)}). Heute: {signals_today['count']}/{MAX_SIGNALS_DAY}")
        elif rejs:
            scan_stats["summary"] = (f"🔍 Scan {t}: kein Signal. Geprüft/verworfen: "
                                     f"{', '.join(rejs[:6])}. Nächster in {SCAN_INTERVAL_MIN} Min.")
        else:
            scan_stats["summary"] = (f"🔍 Scan {t}: keine passenden Setups bei den {len(PAIRS)} Paaren. "
                                     f"Nächster in {SCAN_INTERVAL_MIN} Min.")
        print(f"[{datetime.now():%H:%M}] Scan fertig. Heute: {signals_today['count']}/{MAX_SIGNALS_DAY}")

# ═══════════════════════════════════════════════════════════════
#  KI-CHAT
# ═══════════════════════════════════════════════════════════════
def ai_chat(user_msg: str, deep: bool = False) -> str:
    """
    Beantwortet Nutzerfragen. Standard: Sonnet 4.6 (günstig).
    deep=True: Opus 4.8 für premium-recherchierte Antworten (per /deep).
    """
    global chat_history
    chat_history.append({"role": "user", "content": user_msg})
    if len(chat_history) > 16:
        chat_history = chat_history[-16:]
    model = AI_MODEL_DEEP if deep else AI_MODEL_CHAT
    max_uses = 6 if deep else 3
    sys = ("Du bist ein erfahrener Forex-Daytrading-Assistent. Antworte auf Deutsch, "
           "präzise und sachlich. Du darfst live im Web recherchieren (Kurse, News, Kalender, "
           "Sentiment). Du gibst klare Einschätzungen mit Begründung, weist auf Risiken hin, "
           "und machst keine Garantieversprechen. Halte Antworten Telegram-tauglich kurz.")
    try:
        resp = anthropic.messages.create(
            model=model, max_tokens=900,
            system=[{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
            messages=chat_history,
        )
        answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        chat_history.append({"role": "assistant", "content": answer})
        return answer or "Konnte keine Antwort erzeugen."
    except Exception as e:
        if chat_history and chat_history[-1]["role"] == "user":
            chat_history.pop()
        return f"❌ KI nicht erreichbar: {e}"

# ═══════════════════════════════════════════════════════════════
#  OANDA-TRADING (Demo standardmäßig) + Button-Flow
# ═══════════════════════════════════════════════════════════════
pending_trades: dict = {}        # short_id -> Trade-Parameter (für Accept-Button)
awaiting_lot: dict = {}          # chat_id -> short_id (Nutzer gibt gleich Lot ein)
_trade_counter = 0

def oanda_instrument(pair: str) -> str:
    """EUR/USD -> EUR_USD (OANDA-Format)."""
    return pair.replace("/", "_")

def oanda_configured() -> bool:
    return bool(OANDA_TOKEN and OANDA_ACCOUNT_ID)

def oanda_place_order(instrument: str, units: int, entry: float, sl: float,
                      tp: float, digits: int) -> dict:
    """
    Platziert eine LIMIT-Order auf dem (Demo-)Konto mit SL & TP.
    units: positiv = Buy, negativ = Sell. Gibt {ok, id/err} zurück.
    """
    url = f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    headers = {"Authorization": f"Bearer {OANDA_TOKEN}", "Content-Type": "application/json"}
    body = {"order": {
        "type": "LIMIT",
        "instrument": instrument,
        "units": str(int(units)),
        "price": f"{entry:.{digits}f}",
        "timeInForce": "GTC",
        "positionFill": "DEFAULT",
        "stopLossOnFill": {"price": f"{sl:.{digits}f}", "timeInForce": "GTC"},
        "takeProfitOnFill": {"price": f"{tp:.{digits}f}"},
    }}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
        data = r.json()
        if r.status_code in (200, 201) and ("orderCreateTransaction" in data
                                            or "orderFillTransaction" in data):
            txid = (data.get("orderCreateTransaction", {}) or {}).get("id", "?")
            return {"ok": True, "id": txid}
        # OANDA liefert Fehlerdetails
        msg = data.get("errorMessage") or data.get("orderRejectTransaction", {}).get("reason") or str(data)[:200]
        return {"ok": False, "err": msg}
    except Exception as e:
        return {"ok": False, "err": str(e)}

def execute_trade_oanda(t: dict) -> str:
    """Öffnet pro Take-Profit eine Teil-Order (Ausskalieren) über OANDA."""
    pair = t["pair"]
    instrument = oanda_instrument(pair)
    digits = 3 if ("JPY" in pair or pair.startswith("XAU")) else 5
    tps = t["take_profits"]
    total_units = t["lots"] * 100000
    if t["direction"] == "short":
        total_units = -total_units
    per = int(round(total_units / len(tps)))
    if per == 0:
        return "❌ Lot zu klein für eine Teilung auf mehrere TPs. Erhöhe die Lot."

    lines, ok_count = [], 0
    for i, tp in enumerate(tps, 1):
        res = oanda_place_order(instrument, per, t["entry"], t["stop_loss"], tp, digits)
        if res["ok"]:
            ok_count += 1
            lines.append(f"  ✅ Teil-Trade {i}/{len(tps)} → TP{i} (Order {res['id']})")
        else:
            lines.append(f"  ❌ Teil-Trade {i}: {res['err']}")
    head = (f"📈 {pair} {t['direction'].upper()} — {ok_count}/{len(tps)} Orders platziert "
            f"({OANDA_ENV})\n")
    return head + "\n".join(lines)

# ─── Capital.com ───
CAPITAL_EPICS = {"XAU/USD": "GOLD", "XAG/USD": "SILVER"}

def capital_configured() -> bool:
    return bool(CAPITAL_API_KEY and CAPITAL_IDENTIFIER and CAPITAL_PASSWORD)

def capital_epic(pair: str) -> str:
    """EUR/USD -> EURUSD, XAU/USD -> GOLD."""
    return CAPITAL_EPICS.get(pair, pair.replace("/", ""))

def capital_digits(pair: str) -> int:
    if pair.startswith("XAU"): return 2
    if "JPY" in pair: return 3
    return 5

def capital_login():
    """Session erstellen → (cst, x-security-token, fehler)."""
    url = f"{CAPITAL_BASE}/api/v1/session"
    headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}
    body = {"identifier": CAPITAL_IDENTIFIER, "password": CAPITAL_PASSWORD}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
        if r.status_code != 200:
            return None, None, f"Login fehlgeschlagen ({r.status_code}): {r.text[:150]}"
        cst = r.headers.get("CST")
        xsec = r.headers.get("X-SECURITY-TOKEN")
        if not cst or not xsec:
            return None, None, "Keine Session-Tokens erhalten (2FA aktiviert?)"
        return cst, xsec, None
    except Exception as e:
        return None, None, str(e)

def capital_place_order(cst, xsec, epic, direction, size, level, sl, tp, digits,
                        order_type="LIMIT") -> dict:
    """Platziert eine Order mit SL & TP über Capital.com.
    order_type LIMIT = Working-Order auf Pullback-Level; MARKET = sofort zum Marktpreis."""
    headers = {"CST": cst, "X-SECURITY-TOKEN": xsec, "Content-Type": "application/json"}
    if order_type == "MARKET":
        url = f"{CAPITAL_BASE}/api/v1/positions"
        body = {
            "epic": epic, "direction": direction, "size": size,
            "guaranteedStop": False,
            "stopLevel": round(sl, digits), "profitLevel": round(tp, digits),
        }
    else:
        url = f"{CAPITAL_BASE}/api/v1/workingorders"
        body = {
            "epic": epic, "direction": direction, "size": size,
            "level": round(level, digits), "type": "LIMIT",
            "stopLevel": round(sl, digits), "profitLevel": round(tp, digits),
            "guaranteedStop": False,
        }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
        data = r.json() if r.content else {}
        if r.status_code in (200, 201) and data.get("dealReference"):
            return {"ok": True, "id": data["dealReference"]}
        msg = data.get("errorCode") or data.get("error") or r.text[:150]
        return {"ok": False, "err": msg}
    except Exception as e:
        return {"ok": False, "err": str(e)}

def capital_min_size(cst, xsec, epic) -> float | None:
    """Fragt die minimale Deal-Größe für ein Instrument ab (Capital.com)."""
    url = f"{CAPITAL_BASE}/api/v1/markets/{epic}"
    headers = {"CST": cst, "X-SECURITY-TOKEN": xsec, "Content-Type": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            rules = (r.json() or {}).get("dealingRules", {}) or {}
            v = (rules.get("minDealSize", {}) or {}).get("value")
            return float(v) if v is not None else None
    except Exception:
        pass
    return None

def capital_deal_id(cst, xsec, deal_ref: str):
    """Löst eine dealReference in die echte Position-ID (dealId) auf."""
    url = f"{CAPITAL_BASE}/api/v1/confirms/{deal_ref}"
    headers = {"CST": cst, "X-SECURITY-TOKEN": xsec}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            d = r.json() or {}
            ad = d.get("affectedDeals") or []
            if ad and ad[0].get("dealId"):
                return ad[0]["dealId"]
            return d.get("dealId")
    except Exception:
        pass
    return None

def capital_update_stop(cst, xsec, deal_id: str, stop_level: float,
                        profit_level: float | None, digits: int) -> bool:
    """Verschiebt den Stop einer offenen Position und BEHÄLT den Take-Profit.
    Prüft die Bestätigung (Amend ist asynchron → HTTP 200 allein reicht nicht)."""
    url = f"{CAPITAL_BASE}/api/v1/positions/{deal_id}"
    headers = {"CST": cst, "X-SECURITY-TOKEN": xsec, "Content-Type": "application/json"}
    body = {"stopLevel": round(stop_level, digits)}
    if profit_level is not None:
        body["profitLevel"] = round(profit_level, digits)   # TP MITSCHICKEN, sonst löscht Capital ihn
    try:
        r = requests.put(url, headers=headers, json=body, timeout=15)
        if r.status_code not in (200, 201):
            return False
        ref = (r.json() or {}).get("dealReference")
        if not ref:
            return True
        # Bestätigung prüfen — nur bei explizitem REJECTED als Fehler werten
        for _ in range(3):
            c = requests.get(f"{CAPITAL_BASE}/api/v1/confirms/{ref}", headers=headers, timeout=15)
            if c.status_code == 200:
                d = c.json() or {}
                status = str(d.get("dealStatus") or d.get("status") or "").upper()
                return status != "REJECTED"
            _time.sleep(0.4)
        return True
    except Exception:
        return False

def capital_place_order_grow(cst, xsec, epic, direction, size, level, sl, tp, digits) -> dict:
    """
    Platziert eine MARKT-Order (Einstieg sofort) mit Stop UND Take-Profit.
    Markt-Order, weil dort stopLevel + profitLevel zuverlässig zusammen gesetzt werden
    (bei Limit-Working-Orders wird der TP von Capital nicht übernommen).
    Vergrößert die Size automatisch, falls Capital sie als zu klein ablehnt.
    """
    s = max(float(size), 0.01)
    last = ""
    for _ in range(9):
        res = capital_place_order(cst, xsec, epic, direction, round(s, 2),
                                  level, sl, tp, digits, "MARKET")
        if res["ok"]:
            res["size"] = round(s, 2)
            res["type"] = "MARKET"
            return res
        last = str(res.get("err", "")).lower()
        if "minvalue" in last or ("min" in last and "size" in last):
            s = s * 2 if s > 0 else 1.0          # zu klein → verdoppeln
            continue
        return res                                # anderer Fehler → abbrechen
    return {"ok": False, "err": last}

def execute_trade_capital(t: dict) -> str:
    """Öffnet pro Take-Profit eine Teil-Order über Capital.com — direkt in Capital-Size."""
    cst, xsec, err = capital_login()
    if err:
        return f"❌ Capital.com Login fehlgeschlagen: {err}"
    pair = t["pair"]
    epic = capital_epic(pair)
    digits = capital_digits(pair)
    direction = "BUY" if t["direction"] == "long" else "SELL"
    tps = t["take_profits"]

    # Größe je Teil-Order: Risiko-Basis, aufs Broker-Minimum heben, DANN mit dem Größen-Faktor skalieren
    per = round(t["lots"] / len(tps), 2)
    min_size = capital_min_size(cst, xsec, epic) or CAPITAL_MIN_SIZE
    if min_size and per < min_size:
        per = min_size
    per = round(max(per, 0.01) * CAPITAL_SIZE_FACTOR, 2)   # ← Größen-Hebel wirkt jetzt wirklich

    lines, ok_count, used, order_results = [], 0, None, []
    for i, tp in enumerate(tps, 1):
        start = used if used else per          # nach 1. Treffer gleiche Size weiterverwenden
        res = capital_place_order_grow(cst, xsec, epic, direction, start,
                                       t["entry"], t["stop_loss"], tp, digits)
        if res["ok"]:
            ok_count += 1
            used = res["size"]
            order_results.append((res["id"], tp))     # Order-Ref + zugehöriger TP
            typ = "Limit" if res.get("type") == "LIMIT" else "Markt"
            lines.append(f"  ✅ Teil-Order {i}/{len(tps)} → TP{i} | {typ} | Size {res['size']} (Ref {res['id'][:10]})")
        else:
            lines.append(f"  ❌ Teil-Order {i}: {res['err']}")

    # Position-IDs auflösen und mit ihrem TP speichern (fürs Breakeven, TP bleibt erhalten)
    deal_map = []
    for ref, tp in order_results:
        did = capital_deal_id(cst, xsec, ref)
        if did:
            deal_map.append({"id": did, "tp": round(tp, digits)})

    head = (f"📈 {pair} {direction} ({epic}) — {ok_count}/{len(tps)} Orders ({CAPITAL_ENV})\n")
    # Order-Infos fürs Dashboard + Breakeven merken
    last_exec.clear()
    last_exec.update({"size": used, "orders_ok": ok_count, "orders_total": len(tps),
                      "order_type": "Markt", "deal_map": deal_map})
    note = ""
    if ok_count and used and used > per + 0.001:
        note = (f"\n\nℹ️ Größe automatisch auf {used} angehoben (Capital-Mindestgröße). "
                f"Größer/kleiner? Größe per Button ändern oder CAPITAL_SIZE_FACTOR setzen.")
    elif ok_count:
        note = "\n\n⚠️ Prüfe in der Capital.com-App, ob die Größe passt (Button/CAPITAL_SIZE_FACTOR)."
    return head + "\n".join(lines) + note


def broker_configured() -> bool:
    return capital_configured() if BROKER == "capital" else oanda_configured()

def broker_env_label() -> str:
    return CAPITAL_ENV if BROKER == "capital" else OANDA_ENV

def execute_trade(t: dict) -> str:
    """Dispatcht an den gewählten Broker."""
    if BROKER == "capital":
        return execute_trade_capital(t)
    return execute_trade_oanda(t)


def trade_buttons(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept", callback_data=f"acc:{sid}"),
         InlineKeyboardButton("❌ Decline", callback_data=f"dec:{sid}")],
        [InlineKeyboardButton("✏️ LOT bearbeiten", callback_data=f"lot:{sid}")],
    ])

async def send_signal_with_buttons(bot: Bot, pair: str, s: dict, htf: dict | None, rid: str | None = None):
    """Sendet das Signal. Auto-Trade führt direkt aus; sonst mit Accept-Buttons; ohne Broker nur Text."""
    global _trade_counter
    text = format_signal(pair, s, htf)
    if not (TRADING_ENABLED and broker_configured()):
        await tg_send(bot, text)
        return

    sl_pips = pips_between(pair, s["entry"], s["stop_loss"])
    lots, _, _ = suggest_lot(s["confidence"], sl_pips, pair, s["entry"])
    t = {
        "pair": pair, "direction": s["direction"], "entry": s["entry"],
        "stop_loss": s["stop_loss"], "take_profits": s["take_profits"], "lots": lots,
        "log_id": rid,
    }

    # ── AUTO-TRADE: Signal senden + sofort ausführen (wie ein automatisches Accept) ──
    if AUTO_TRADE:
        await tg_send(bot, text + f"\n\n⚡ Auto-Trade aktiv — platziere Order automatisch ({broker_env_label()})...")
        status = await asyncio.to_thread(execute_trade, t)
        if rid and last_exec.get("orders_ok"):
            mark_trade_opened(rid)
        await tg_send(bot, status)
        return

    # ── Sonst: Signal mit Buttons, Ausführung erst nach deinem Accept ──
    _trade_counter += 1
    sid = str(_trade_counter)
    pending_trades[sid] = t
    if not TELEGRAM_CHAT_ID:
        await tg_send(bot, text)
        return
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text + "\n\n👇 Was tun?",
                           reply_markup=trade_buttons(sid))

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Verarbeitet Accept / Decline / LOT bearbeiten."""
    q = update.callback_query
    await q.answer()
    try:
        action, sid = q.data.split(":", 1)
    except ValueError:
        return
    t = pending_trades.get(sid)
    if not t:
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("⚠️ Dieses Signal ist abgelaufen (Bot wurde evtl. neu gestartet).")
        return

    if action == "dec":
        pending_trades.pop(sid, None)
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"❌ {t['pair']} verworfen.")
        return

    if action == "lot":
        awaiting_lot[q.message.chat_id] = sid
        await q.message.reply_text(
            f"✏️ Sende mir jetzt die gewünschte Lot-Größe für {t['pair']} "
            f"(aktuell {t['lots']:.2f}). Beispiel: 0.5")
        return

    if action == "acc":
        if not (TRADING_ENABLED and broker_configured()):
            await q.message.reply_text(
                f"⚠️ Trading ist nicht aktiv. Setze die {BROKER.capitalize()}-Variablen "
                f"und TRADING_ENABLED=true in Railway.")
            return
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"⏳ Platziere Orders für {t['pair']} ({broker_env_label()})...")
        status = await asyncio.to_thread(execute_trade, t)
        # Im Dashboard als "von dir geöffnet" markieren (mit echter Order-Größe)
        if t.get("log_id") and last_exec.get("orders_ok"):
            mark_trade_opened(t["log_id"])
        pending_trades.pop(sid, None)
        await q.message.reply_text(status)

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 Forex Signal Bot (Opus 4.8)\n"
        f"Chat-ID: {update.effective_chat.id}\n\n"
        f"Überwacht: {', '.join(PAIRS)}\n"
        f"Max {MAX_SIGNALS_DAY} Signale/Tag, nur Confidence ≥ {CONFIDENCE_MIN}/100\n\n"
        f"/status — Status & Session\n/scan — Sofort-Scan\n/today — Tagesübersicht\n"
        f"/lot — Positionsgröße berechnen\n/deep <frage> — Premium-Recherche\n/pairs — Paare\n/help — Hilfe\n\n"
        f"💬 Normale Fragen → günstig (Sonnet). Wichtige → /deep (Opus 4.8)")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 So funktioniert der Bot:\n\n"
        f"Er scannt die {len(PAIRS)} Paare im {INTERVAL}-Takt (Scalping). Die Signale entstehen aus einer "
        f"kostenlosen technischen Engine: Trend (EMA), RSI, MACD, ADX-Trendstärke, ATR-Stop, Swing-Ziele und "
        f"{HTF_INTERVAL}-Multi-Timeframe-Bestätigung. Kein KI-Aufruf pro Signal = keine laufenden Kosten.\n\n"
        f"Nur Signale mit Confidence ≥ {CONFIDENCE_MIN} und Chance/Risiko ≥ 1:{MIN_RR} werden geschickt. "
        f"Stop-Loss und Take-Profit kommen aus echter Marktstruktur (ATR + Swing-Levels), nicht aus festen Prozenten. "
        f"Das Chance/Risiko wird aus den echten Levels nachgerechnet.\n\n"
        "Es kann gut sein, dass mal ein ganzer Tag kein Signal kommt — das ist Absicht.\n\n"
        "/status — Markt, Session, Schwellen\n/scan — manuell scannen\n/today — was heute lief\n"
        "/backtest [Tage] — range-Strategie auf echter Historie testen (Standard 60 Tage)\n"
        "/lot — Positionsgröße aus Risiko berechnen\n/deep <frage> — Premium-Recherche (Opus 4.8)\n/pairs — überwachte Paare\n\n"
        "Normale Fragen beantworte ich günstig mit Sonnet 4.6 (inkl. Websuche).\n"
        "Für die wichtigen Fragen nutz /deep — das läuft auf Opus 4.8.\n\n"
        "Frag mich z.B.:\n„Was ist gerade los bei EUR/USD?“\n„Welche News stehen heute an?“")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_daily_if_needed()
    mo = "🟢 offen" if market_open() else "🔴 geschlossen"
    env = broker_env_label()
    is_demo = env in ("demo", "practice")
    if not broker_configured():
        trade_mode = f"❌ aus (kein {BROKER.capitalize()})"
    elif not TRADING_ENABLED:
        trade_mode = "❌ aus (TRADING_ENABLED=false)"
    else:
        trade_mode = f"✅ AN über {BROKER.capitalize()} — {'🟡 DEMO' if is_demo else '🔴 LIVE/ECHT'}"
    if TRADING_ENABLED and broker_configured():
        trade_mode += "\n⚡ Auto-Trade: " + ("AN (Signale werden automatisch ausgeführt)"
                                             if AUTO_TRADE else "aus (Bestätigung per Accept)")
        trade_mode += "\n🔒 Breakeven: " + (f"ab +{BREAKEVEN_AT_R}R Stop auf Einstieg"
                                            if BREAKEVEN_ENABLED else "aus")
        trade_mode += "\n📈 Trailing-Stop: " + (f"an ({TRAIL_ATR_MULT}×ATR)" if TRAIL_ENABLED else "aus")
    analyse = "KI (Sonnet+Suche)" if USE_AI_ANALYSIS else "Technik (kostenlos)"
    await update.message.reply_text(
        f"📊 Status\nMarkt: {mo}\n"
        f"🕐 Session: {current_session()}\n"
        f"Analyse: {analyse}\n"
        f"Signale heute: {signals_today['count']}/{MAX_SIGNALS_DAY}\n"
        f"Überwachte Paare: {len(PAIRS)} | Chart {INTERVAL}, HTF {HTF_INTERVAL}\n"
        f"Confidence-Schwelle: {CONFIDENCE_MIN}/100 | Min R:R 1:{MIN_RR}\n"
        f"💹 Trading: {trade_mode}")

async def cmd_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Überwachte Major-Paare:\n" + "\n".join(f"• {p}" for p in PAIRS))

async def cmd_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Backtest der range-Strategie auf echter Historie (Standard: ~60 Tage)."""
    days = 60
    if ctx.args:
        try:
            days = max(14, min(120, int(ctx.args[0])))
        except ValueError:
            pass
    await update.message.reply_text(
        f"🧪 Backtest läuft: range-Strategie, {len(PAIRS)} Paare, ~{days} Tage (1h + 4h-Filter).\n"
        f"⏳ {len(PAIRS) * 2} Datenabrufe mit API-Drossel — dauert ca. 2–3 Minuten...")
    try:
        txt = await asyncio.to_thread(run_backtest, days)
    except Exception as e:
        txt = f"❌ Backtest-Fehler: {e}"
    await update.message.reply_text(txt[:4000])

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if scan_lock.locked():
        await update.message.reply_text("⏳ Ein Scan läuft bereits, bitte kurz warten.")
        return
    await update.message.reply_text("🔍 Starte Sofort-Scan... (kann 1-2 Min dauern)")
    await scan(ctx.application.bot)
    # Ergebnis direkt zurückmelden (kein /today nötig)
    summary = scan_stats.get("summary") or "🔍 Scan abgeschlossen."
    await update.message.reply_text(summary)

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_daily_if_needed()
    last = scan_stats.get("last_run")
    last_str = "noch kein Scan"
    if last:
        dt = datetime.fromisoformat(last)
        last_str = dt.strftime("%H:%M")
    rej = scan_stats.get("rejections", [])
    rej_str = "\n".join(f"  • {r}" for r in rej[:10]) if rej else "  • keine"
    await update.message.reply_text(
        f"📋 HEUTE\n══════════════════\n"
        f"Signale gesendet: {signals_today['count']}/{MAX_SIGNALS_DAY}\n"
        f"Letzter Scan: {last_str}\n"
        f"Kandidaten zuletzt: {scan_stats.get('candidates',0)}\n\n"
        f"Zuletzt abgelehnt:\n{rej_str}")

async def cmd_lot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/lot <kontogröße> <risiko%> <sl_pips> [paar]"""
    if len(ctx.args) < 3:
        await update.message.reply_text(
            "💰 Positionsgrößen-Rechner\n\n"
            "Nutzung:\n/lot <kontogröße> <risiko%> <sl_pips> [paar]\n\n"
            "Beispiel:\n/lot 1000 2 30 EUR/USD\n"
            "= 1000€ Konto, 2% Risiko, 30 Pips Stop\n\n"
            "Bei USD-Paaren reicht es ohne Paar-Angabe.")
        return
    account = safe_float(ctx.args[0])
    risk_pct = safe_float(ctx.args[1])
    sl_pips = safe_float(ctx.args[2])
    pair = ctx.args[3].upper() if len(ctx.args) >= 4 else "EUR/USD"
    if None in (account, risk_pct, sl_pips) or sl_pips <= 0 or account <= 0:
        await update.message.reply_text("❌ Ungültige Werte. Beispiel: /lot 1000 2 30 EUR/USD")
        return

    risk_amount = account * risk_pct / 100

    # Pip-Wert pro Standard-Lot (100k Einheiten), grob in Kontowährung≈USD
    if pair.endswith("USD"):
        pip_val_lot = 10.0
    elif pair.startswith("USD"):
        price = await asyncio.to_thread(fetch_price, pair) or 0
        pip_val_lot = (pip_size(pair) / price) * 100000 if price > 0 else 10.0
    else:
        pip_val_lot = 10.0

    lots = risk_amount / (sl_pips * pip_val_lot)
    units = lots * 100000

    # Positionswert (Notional) grob in Kontowährung + impliziter Hebel
    if pair.endswith("USD"):
        notional = units                       # z.B. EUR/USD: units in Basis ≈ Wert
    elif pair.startswith("XAU"):
        notional = units                       # grob
    else:
        notional = units                       # Näherung
    leverage = notional / account if account > 0 else 0

    lev_warn = ""
    if leverage > 10:
        lev_warn = f"\n⚠️ Hoher Hebel ({leverage:.0f}×)! Erwäge weniger Risiko-% oder einen weiteren Stop."

    note = ""
    if pair.startswith(("XAU", "XAG")):
        note = "\nℹ️ Metalle: Lot-Konventionen variieren je Broker — nur grobe Orientierung."

    await update.message.reply_text(
        f"💰 POSITIONSGRÖSSE — {pair}\n"
        f"══════════════════════\n"
        f"Konto: {account:,.0f} | Risiko: {risk_pct:.1f}% = {risk_amount:,.0f}\n"
        f"Stop-Loss: {sl_pips:.0f} Pips\n\n"
        f"➡️ {lots:.2f} Lots\n"
        f"   = {lots*10:.1f} Mini-Lots\n"
        f"   = {lots*100:.0f} Mikro-Lots\n"
        f"   = {units:,.0f} Einheiten\n\n"
        f"📊 Positionswert: ~{notional:,.0f} ({leverage:.1f}× Hebel aufs Konto)\n"
        f"💸 Verlust bei SL-Hit: ~{risk_amount:,.0f} ({risk_pct:.1f}%)"
        f"{lev_warn}\n\n"
        f"ℹ️ \"Lot\" = Positionsgröße, NICHT dein Risiko. 1 Lot = 100.000 Einheiten "
        f"(~10$/Pip). Dein echtes Risiko = Lots × Pip-Wert × Stop. Ein enger Stop "
        f"erlaubt mehr Lots bei gleichem Risiko, erhöht aber den Hebel.\n"
        f"⚠️ Näherung; je nach Broker/Kontowährung leicht abweichend.{note}")

async def cmd_deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/deep <frage> — premium-recherchierte Antwort über Opus 4.8."""
    if not ctx.args:
        await update.message.reply_text(
            "🔬 Premium-Recherche (Opus 4.8)\n\n"
            "Nutzung: /deep <deine Frage>\n"
            "Beispiel: /deep Wie ist der Ausblick für EUR/USD diese Woche?\n\n"
            "Kostet mehr als normale Fragen — nutz es für die wichtigen.")
        return
    question = " ".join(ctx.args)
    thinking = await update.message.reply_text("🔬 Tiefen-Recherche mit Opus 4.8...")
    answer = await asyncio.to_thread(ai_chat, question, True)
    try:
        await thinking.edit_text(answer)
    except Exception:
        await update.message.reply_text(answer)

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    chat_id = update.message.chat_id

    # Wartet der Nutzer gerade auf Lot-Eingabe für ein Signal?
    if chat_id in awaiting_lot:
        sid = awaiting_lot.pop(chat_id)
        t = pending_trades.get(sid)
        new_lot = safe_float(msg.replace(",", "."))
        if not t:
            await update.message.reply_text("⚠️ Signal ist abgelaufen.")
            return
        if new_lot is None or new_lot <= 0:
            awaiting_lot[chat_id] = sid  # nochmal versuchen
            await update.message.reply_text("❌ Ungültig. Schick eine Zahl, z.B. 0.5")
            return
        t["lots"] = round(new_lot, 2)
        await update.message.reply_text(
            f"✏️ Lot für {t['pair']} auf {t['lots']:.2f} gesetzt "
            f"(aufgeteilt auf {len(t['take_profits'])} Teil-Trades je {t['lots']/len(t['take_profits']):.2f}).",
            reply_markup=trade_buttons(sid))
        return

    thinking = await update.message.reply_text("🤔 Recherchiere...")
    answer = await asyncio.to_thread(ai_chat, msg)
    try:
        await thinking.edit_text(answer)
    except Exception:
        await update.message.reply_text(answer)

# ═══════════════════════════════════════════════════════════════
#  SCANNER LOOP
# ═══════════════════════════════════════════════════════════════
async def scanner_loop(bot: Bot):
    while True:
        try:
            await scan(bot)
            # Sofort-Meldung nur bei echter Aktivität (Setup analysiert), kein Spam bei leeren Scans.
            # Bei gesendeten Signalen ist das Signal selbst schon die Meldung.
            if (not scan_stats.get("closed_market")
                    and not scan_stats.get("signals_this_run")
                    and scan_stats.get("ai_calls", 0) > 0
                    and scan_stats.get("summary")):
                await tg_send(bot, scan_stats["summary"])
        except Exception as e:
            print(f"[SCAN ERROR] {e}")
        await asyncio.sleep(SCAN_INTERVAL_MIN * 60)

# ═══════════════════════════════════════════════════════════════
#  DASHBOARD-WEBSERVER (liefert Dashboard-HTML + JSON-Daten)
# ═══════════════════════════════════════════════════════════════
def dashboard_payload() -> dict:
    """Alle Daten fürs Dashboard (Statistiken rechnet das Frontend)."""
    snapshot = list(trade_log)   # Kopie, da der Bot-Thread parallel schreibt
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategy": STRATEGY,
        "interval": INTERVAL,
        "htf": HTF_INTERVAL,
        "broker": BROKER,
        "env": (CAPITAL_ENV if BROKER == "capital" else OANDA_ENV),
        "trading_enabled": bool(TRADING_ENABLED and broker_configured()),
        "auto_trade": bool(AUTO_TRADE and TRADING_ENABLED and broker_configured()),
        "breakeven": (BREAKEVEN_AT_R if BREAKEVEN_ENABLED else None),
        "trailing": (TRAIL_ATR_MULT if TRAIL_ENABLED else None),
        "pairs": PAIRS,
        "signals_today": signals_today,
        "max_signals_day": MAX_SIGNALS_DAY,
        "confidence_min": CONFIDENCE_MIN,
        "min_rr": MIN_RR,
        "size_factor": CAPITAL_SIZE_FACTOR,
        "trades": snapshot,
    }

def _read_dashboard_html() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.getenv("DASHBOARD_FILE", "dashboard.html"),
              os.path.join(here, "dashboard.html")):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read()
        except Exception:
            continue
    return ("<html><body style='font-family:sans-serif;background:#111;color:#eee;padding:2rem'>"
            "<h1>Dashboard-Datei fehlt</h1><p>Lege <code>dashboard.html</code> ins Repo "
            "(gleicher Ordner wie bot.py).</p></body></html>")

class DashHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/dashboard", "/index.html"):
            body = _read_dashboard_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors(); self.end_headers(); self.wfile.write(body)
        elif path in ("/api/data", "/data", "/api/trades"):
            try:
                body = json.dumps(dashboard_payload()).encode("utf-8")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors(); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self._cors(); self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, *args):
        pass   # kein HTTP-Logspam in den Railway-Logs

def start_dashboard_server():
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", DASHBOARD_PORT), DashHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"[Dashboard] Webserver läuft auf Port {DASHBOARD_PORT}  (/  und  /api/data)")
    except Exception as e:
        print(f"[Dashboard] Server-Start fehlgeschlagen: {e}")

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
async def main():
    print("=" * 55)
    print("  📊 Forex Signal Bot — OPUS 4.8")
    mode = "KI (Sonnet+Suche)" if USE_AI_ANALYSIS else "Technik (kostenlos)"
    print(f"  Paare: {len(PAIRS)} | Analyse: {mode} | Chat: {AI_MODEL_CHAT} | Deep: {AI_MODEL_DEEP}")
    print("=" * 55)
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN fehlt!"); return
    if not TWELVE_KEY:
        print("⚠️ TWELVE_DATA_KEY fehlt — keine Kursdaten möglich!")

    load_state()
    load_trade_log()
    reset_daily_if_needed()
    start_dashboard_server()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("lot", cmd_lot))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    bot = app.bot
    await tg_send(bot,
        "📊 Forex Signal Bot gestartet! (Opus 4.8)\n"
        f"Überwache {len(PAIRS)} Paare im {INTERVAL}-Takt (Scalping).\n"
        f"Max {MAX_SIGNALS_DAY} hochsichere Signale/Tag.\n"
        "💬 Stell mir jederzeit Fragen zum Markt!\n\n/help — Infos")

    asyncio.create_task(scanner_loop(bot))

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop(); await app.stop(); await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
