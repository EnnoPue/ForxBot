import asyncio
import json
import os
import re
import requests
import pandas as pd
from datetime import datetime, timezone, date
from anthropic import Anthropic
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

import indicators as ind

# ═══════════════════════════════════════════════════════════════
#  CONFIG  (Railway Environment Variables)
# ═══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
TWELVE_KEY       = os.getenv("TWELVE_DATA_KEY", "")
AI_MODEL         = os.getenv("AI_MODEL", "claude-opus-4-8")

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD"]
INTERVAL          = "1h"        # Daytrading-Zeitfenster
PRESCREEN_MIN     = 60          # Technischer Vorab-Filter
CONFIDENCE_MIN    = 75          # KI-Confidence-Schwelle für ein echtes Signal
MAX_SIGNALS_DAY   = 3           # max. Signale pro Tag
MIN_RR            = 1.5         # Mindest-Chance-Risiko-Verhältnis
SCAN_INTERVAL_MIN = 90          # Scan-Takt während Marktzeiten
SIGNAL_COOLDOWN_H = 6           # selbes Paar nicht öfter als alle 6h
STATE_FILE        = "state.json"

anthropic = Anthropic(api_key=ANTHROPIC_KEY)
scan_lock = asyncio.Lock()

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
    s["entry"], s["stop_loss"], s["take_profit"] = entry, sl, tp
    s["confidence"] = conf
    s["risk_reward"] = round(reward / risk, 2)   # autoritativ, neu berechnet
    return s

# ═══════════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════════
chat_history: list = []
signals_today = {"date": str(date.today()), "count": 0}
last_signal_time: dict = {}   # pair -> ISO timestamp
scan_stats = {"last_run": None, "candidates": 0, "rejections": []}

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"signals_today": signals_today, "last_signal_time": last_signal_time}, f)
    except Exception as e:
        print(f"[STATE save] {e}")

def load_state():
    global signals_today, last_signal_time
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        signals_today = d.get("signals_today", signals_today)
        last_signal_time = d.get("last_signal_time", {})
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
    url = f"https://api.twelvedata.com/price?symbol={pair}&apikey={TWELVE_KEY}"
    try:
        r = requests.get(url, timeout=15)
        return safe_float(r.json().get("price"))
    except Exception:
        return None

def htf_trend(pair: str) -> dict | None:
    """Übergeordneter 4h-Trend für Multi-Timeframe-Confluence."""
    df = fetch_ohlc(pair, interval="4h", size=250)
    if df is None or len(df) < 210:
        return None
    return ind.analyze(df)

# ═══════════════════════════════════════════════════════════════
#  PIP-HELFER
# ═══════════════════════════════════════════════════════════════
def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001

def pips_between(pair: str, a: float, b: float) -> float:
    return abs(a - b) / pip_size(pair)

# ═══════════════════════════════════════════════════════════════
#  KI-ANALYSE  (Opus 4.8 mit Live-Websuche)
# ═══════════════════════════════════════════════════════════════
def deep_analysis(pair: str, direction: str, a: dict, htf: dict | None) -> dict | None:
    """
    Lässt Opus 4.8 eine Confluence-Analyse machen:
    Fundamental + Sentiment recherchieren, dann Entry/SL/TP aus echten
    Levels ableiten und begründen. Gibt geparste JSON-Signal zurück.
    """
    ps = pip_size(pair)
    htf_block = "Keine 4h-Daten verfügbar."
    if htf:
        htf_block = (f"4h-Trend: {htf['trend']} (EMA20={htf['ema20']:.5f}, "
                     f"EMA50={htf['ema50']:.5f}), 4h-RSI={htf['rsi']:.1f}, "
                     f"4h-ADX={htf['adx']:.1f}, 4h-Swing-Hoch={htf['swing_high']:.5f}, "
                     f"4h-Swing-Tief={htf['swing_low']:.5f}")
    prompt = f"""Du bist ein professioneller Forex-Daytrader. Analysiere {pair} für einen möglichen {('LONG' if direction=='long' else 'SHORT')}-Trade auf dem 1h-Chart.

TECHNISCHE DATEN 1h (bereits berechnet):
- Aktueller Preis: {a['price']:.5f}
- Trend: {a['trend']} (EMA20={a['ema20']:.5f}, EMA50={a['ema50']:.5f}, EMA200={a['ema200']:.5f})
- RSI(14): {a['rsi']:.1f}
- MACD-Histogramm: {a['macd_hist']:.6f} (vorher {a['macd_hist_prev']:.6f})
- ADX: {a['adx']:.1f} (Trendstärke)
- ATR(14): {a['atr']:.5f}  ← nutze das für die Stop-Distanz (Volatilität)
- Jüngstes Swing-Hoch: {a['swing_high']:.5f}
- Jüngstes Swing-Tief: {a['swing_low']:.5f}
- Pip-Größe: {ps}

ÜBERGEORDNETER 4h-KONTEXT (Multi-Timeframe):
- {htf_block}
- WICHTIG: Handle bevorzugt MIT dem 4h-Trend. Wenn 1h und 4h widersprechen, sei besonders streng oder lehne ab.

DEINE AUFGABE — recherchiere im Web (nutze die Suche aktiv):
1. FUNDAMENTAL: Aktuelle Zinsdifferenz/Notenbank-Haltung der beiden Währungen, jüngste Wirtschaftsdaten, anstehende High-Impact-News in den nächsten 12h (ForexFactory-Kalender). Wenn ein großes Event unmittelbar bevorsteht → Confidence senken oder ablehnen.
2. SENTIMENT: Wie sind die erfolgreichen/großen Marktteilnehmer positioniert? Suche nach COT-Daten (Commitment of Traders), Retail-Sentiment (z.B. IG/Myfxbook), aktuelle Analystenpositionierung.

DANN entscheide:
- Lohnt sich der Trade in Richtung {direction.upper()} wirklich? Sei streng — nur hohe Sicherheit.
- Bestimme Entry (Buy/Sell Limit auf sinnvollem Pullback-Level, nicht einfach Marktpreis).
- Bestimme Stop Loss aus echter Struktur: unter/über dem Swing-Level bzw. ca. 1.5×ATR vom Entry — KEIN fester Prozentsatz. Begründe.
- Bestimme Take Profit am nächsten relevanten Widerstand/Unterstützung (Swing-Level). Das Chance-Risiko-Verhältnis MUSS mindestens {MIN_RR} sein, sonst ablehnen.
- Vergib eine Confidence 0-100. Nur >= {CONFIDENCE_MIN} wird gehandelt.

Bei einem LONG muss gelten: Stop Loss < Entry < Take Profit.
Bei einem SHORT muss gelten: Take Profit < Entry < Stop Loss.

Antworte mit einer kurzen Analyse und am ENDE einem JSON-Block in genau diesem Format (nichts danach):
```json
{{"trade": true/false, "direction": "long/short", "entry": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "confidence": 0-100, "risk_reward": 0.0, "fundamental": "1-2 Sätze", "sentiment": "1-2 Sätze", "reasoning": "warum dieser Entry/SL/TP - 2-3 Sätze"}}
```"""

    try:
        resp = anthropic.messages.create(
            model=AI_MODEL,
            max_tokens=2500,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
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
    digits = 3 if "JPY" in pair else 5
    htf_line = f"\n📐 4h-Trend: {htf['trend']}" if htf else ""

    return (
        f"📊 FOREX SIGNAL — {pair}\n"
        f"══════════════════════\n"
        f"{arrow}\n"
        f"🎯 Entry: {entry:.{digits}f}\n"
        f"🛑 Stop Loss: {sl:.{digits}f}  ({sl_pips:.0f} Pips)\n"
        f"✅ Take Profit: {tp:.{digits}f}  ({tp_pips:.0f} Pips)\n"
        f"⚖️ Chance/Risiko: 1:{s['risk_reward']:.1f}\n"
        f"🎰 Confidence: {s['confidence']:.0f}/100"
        f"{htf_line}\n"
        f"🕐 Session: {current_session()}\n"
        f"\n📰 FUNDAMENTAL\n{s.get('fundamental','-')}\n"
        f"\n👥 SENTIMENT\n{s.get('sentiment','-')}\n"
        f"\n💡 BEGRÜNDUNG\n{s.get('reasoning','-')}\n"
        f"══════════════════════\n"
        f"💰 Positionsgröße? → /lot\n"
        f"⚠️ Kein Finanzrat. Prüfe selbst & nutze Risikomanagement.\n"
        f"💬 Fragen? Schreib mir!"
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

async def scan(bot: Bot):
    if scan_lock.locked():
        print(f"[{datetime.now():%H:%M}] Scan läuft bereits — übersprungen.")
        return
    async with scan_lock:
        reset_daily_if_needed()
        scan_stats["last_run"] = datetime.now().isoformat()
        scan_stats["candidates"] = 0
        scan_stats["rejections"] = []

        if signals_today["count"] >= MAX_SIGNALS_DAY:
            print(f"[{datetime.now():%H:%M}] Tageslimit ({MAX_SIGNALS_DAY}) erreicht.")
            return
        if not market_open():
            print(f"[{datetime.now():%H:%M}] Markt geschlossen.")
            return

        print(f"\n[{datetime.now():%H:%M}] 🔍 Scan {len(PAIRS)} Paare | Session: {current_session()}")
        # ── Stufe 1: technischer Vorfilter (1h) ──
        candidates = []
        for pair in PAIRS:
            if not cooldown_ok(pair):
                continue
            df = await asyncio.to_thread(fetch_ohlc, pair)
            if df is None or len(df) < 210:
                continue
            a = ind.analyze(df)
            score, direction = ind.prescreen_score(a)
            print(f"  {pair}: 1h-prescreen {score}/100 ({direction}, ADX {a['adx']:.0f})")
            if score >= PRESCREEN_MIN and direction != "none":
                candidates.append((pair, direction, a, score))
            await asyncio.sleep(1.5)   # Twelve Data Rate Limit schonen

        # ── Stufe 2: 4h-Multi-Timeframe-Filter ──
        aligned = []
        for pair, direction, a, score in candidates:
            htf = await asyncio.to_thread(htf_trend, pair)
            await asyncio.sleep(1.5)
            if htf:
                # Konflikt mit höherem Zeitfenster → raus
                if direction == "long" and htf["trend"] == "bearish":
                    print(f"  {pair}: verworfen — 1h long vs. 4h bearish")
                    scan_stats["rejections"].append(f"{pair}: 4h-Konflikt")
                    continue
                if direction == "short" and htf["trend"] == "bullish":
                    print(f"  {pair}: verworfen — 1h short vs. 4h bullish")
                    scan_stats["rejections"].append(f"{pair}: 4h-Konflikt")
                    continue
            aligned.append((pair, direction, a, score, htf))

        scan_stats["candidates"] = len(aligned)

        # ── Stufe 3: KI-Tiefenanalyse (beste zuerst) ──
        aligned.sort(key=lambda x: x[3], reverse=True)
        for pair, direction, a, score, htf in aligned:
            if signals_today["count"] >= MAX_SIGNALS_DAY:
                break
            print(f"  → KI-Analyse {pair} ({direction})...")
            raw = await asyncio.to_thread(deep_analysis, pair, direction, a, htf)
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
            if s["risk_reward"] < MIN_RR:
                scan_stats["rejections"].append(f"{pair}: R:R {s['risk_reward']}")
                print(f"    abgelehnt (R:R {s['risk_reward']})")
                continue
            # ── Signal senden ──
            await tg_send(bot, format_signal(pair, s, htf))
            signals_today["count"] += 1
            last_signal_time[pair] = datetime.now().isoformat()
            save_state()
            print(f"    ✅ SIGNAL gesendet ({s['confidence']:.0f}/100, R:R {s['risk_reward']})")

        print(f"[{datetime.now():%H:%M}] Scan fertig. Heute: {signals_today['count']}/{MAX_SIGNALS_DAY}")

# ═══════════════════════════════════════════════════════════════
#  KI-CHAT
# ═══════════════════════════════════════════════════════════════
def ai_chat(user_msg: str) -> str:
    global chat_history
    chat_history.append({"role": "user", "content": user_msg})
    if len(chat_history) > 16:
        chat_history = chat_history[-16:]
    sys = ("Du bist ein erfahrener Forex-Daytrading-Assistent. Antworte auf Deutsch, "
           "präzise und sachlich. Du darfst live im Web recherchieren (Kurse, News, Kalender, "
           "Sentiment). Du gibst klare Einschätzungen mit Begründung, weist auf Risiken hin, "
           "und machst keine Garantieversprechen. Halte Antworten Telegram-tauglich kurz.")
    try:
        resp = anthropic.messages.create(
            model=AI_MODEL, max_tokens=1200,
            system=[{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
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
#  TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 Forex Signal Bot (Opus 4.8)\n"
        f"Chat-ID: {update.effective_chat.id}\n\n"
        f"Überwacht: {', '.join(PAIRS)}\n"
        f"Max {MAX_SIGNALS_DAY} Signale/Tag, nur Confidence ≥ {CONFIDENCE_MIN}/100\n\n"
        f"/status — Status & Session\n/scan — Sofort-Scan\n/today — Tagesübersicht\n"
        f"/lot — Positionsgröße berechnen\n/pairs — Paare\n/help — Hilfe\n\n"
        f"💬 Stell mir jederzeit Fragen zum Markt!")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 So funktioniert der Bot:\n\n"
        f"Er scannt die 7 Major-Paare im 1h-Takt, filtert technisch vor (Trend, RSI, MACD, ADX-Trendstärke), "
        f"prüft den übergeordneten 4h-Trend (Multi-Timeframe) und lässt nur die besten Setups von Opus 4.8 "
        f"analysieren — inkl. Live-Recherche zu Fundamental und Sentiment der großen Trader.\n\n"
        f"Nur Signale mit Confidence ≥ {CONFIDENCE_MIN} und Chance/Risiko ≥ 1:{MIN_RR} werden geschickt. "
        f"Stop-Loss und Take-Profit kommen aus echter Marktstruktur (ATR + Swing-Levels), nicht aus festen Prozenten. "
        f"Das Chance/Risiko wird aus den echten Levels nachgerechnet.\n\n"
        "Es kann gut sein, dass mal ein ganzer Tag kein Signal kommt — das ist Absicht.\n\n"
        "/status — Markt, Session, Schwellen\n/scan — manuell scannen\n/today — was heute lief\n"
        "/lot — Positionsgröße aus Risiko berechnen\n/pairs — überwachte Paare\n\n"
        "Frag mich z.B.:\n„Was ist gerade los bei EUR/USD?“\n„Welche News stehen heute an?“")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_daily_if_needed()
    mo = "🟢 offen" if market_open() else "🔴 geschlossen"
    await update.message.reply_text(
        f"📊 Status (Opus 4.8)\nMarkt: {mo}\n"
        f"🕐 Session: {current_session()}\n"
        f"Signale heute: {signals_today['count']}/{MAX_SIGNALS_DAY}\n"
        f"Überwachte Paare: {len(PAIRS)}\n"
        f"Confidence-Schwelle: {CONFIDENCE_MIN}/100 | Min R:R 1:{MIN_RR}")

async def cmd_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Überwachte Major-Paare:\n" + "\n".join(f"• {p}" for p in PAIRS))

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if scan_lock.locked():
        await update.message.reply_text("⏳ Ein Scan läuft bereits, bitte kurz warten.")
        return
    await update.message.reply_text("🔍 Starte Sofort-Scan... (kann 1-2 Min dauern)")
    await scan(ctx.application.bot)
    await update.message.reply_text("✅ Scan abgeschlossen. (/today für Details)")

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

    await update.message.reply_text(
        f"💰 POSITIONSGRÖSSE — {pair}\n"
        f"══════════════════════\n"
        f"Konto: {account:.0f} | Risiko: {risk_pct:.1f}% = {risk_amount:.2f}\n"
        f"Stop: {sl_pips:.0f} Pips\n\n"
        f"➡️ {lots:.2f} Lots\n"
        f"➡️ {units:,.0f} Einheiten\n"
        f"➡️ Mikrolots: {lots*100:.0f}\n\n"
        f"Bei SL-Hit verlierst du genau {risk_amount:.2f} (≈{risk_pct:.1f}%).\n"
        f"⚠️ Näherung; je nach Broker/Kontowährung leicht abweichend.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
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
        except Exception as e:
            print(f"[SCAN ERROR] {e}")
        await asyncio.sleep(SCAN_INTERVAL_MIN * 60)

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
async def main():
    print("=" * 55)
    print("  📊 Forex Signal Bot — OPUS 4.8")
    print(f"  Paare: {len(PAIRS)} | Modell: {AI_MODEL}")
    print("=" * 55)
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN fehlt!"); return
    if not TWELVE_KEY:
        print("⚠️ TWELVE_DATA_KEY fehlt — keine Kursdaten möglich!")

    load_state()
    reset_daily_if_needed()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("lot", cmd_lot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    bot = app.bot
    await tg_send(bot,
        "📊 Forex Signal Bot gestartet! (Opus 4.8)\n"
        f"Überwache {len(PAIRS)} Major-Paare im 1h-Takt.\n"
        f"Max {MAX_SIGNALS_DAY} hochsichere Signale/Tag.\n"
        "💬 Stell mir jederzeit Fragen zum Markt!\n\n/help — Infos")

    asyncio.create_task(scanner_loop(bot))

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop(); await app.stop(); await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
