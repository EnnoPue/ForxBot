# 📊 Forex Signal Bot (kostenlose Engine)

Forex-Signal-Bot für 10 liquide Paare mit zwei wählbaren Strategien. Signale entstehen aus einer
kostenlosen technischen Engine — kein KI-Aufruf pro Signal, also keine laufenden Kosten.

## Strategien (Variable `STRATEGY`)
- **`orderblock`** (Standard): Smart-Money-Methode auf 1h. Erkennt einen Break of Structure
  (jüngstes Swing-Hoch/Tief gebrochen) + den Order-Block (letzte gegenläufige Kerze vor dem
  Impuls) und gibt nur ein Signal, wenn der Preis in die Order-Block-Zone **zurückkehrt** (Retest).
  **Strukturelle Stops hinter dem Block** (breiter als ATR-Scalp-Stops → weniger Noise-Ausstoppen),
  Ziele an der Liquidität. Zeitfenster 1h, übergeordneter Trend 4h.
- **`technical`**: die alte 15min-Engine (EMA/RSI/MACD/ADX-Confluence, Pullback-Entry, ATR-Stop).

## Wie es funktioniert (orderblock)
1. Lädt 1h-Kursdaten (Twelve Data) für 10 liquide Paare (Majors + EUR/JPY, GBP/JPY, Gold)
2. Berechnet lokal: EMA(20/50/200), RSI, MACD, ATR, ADX, Bollinger, Swing-Levels
3. Erkennt Order-Block + Break of Structure + Retest der Zone (sonst kein Signal)
4. 4h-Multi-Timeframe-Check (nur für Top-Kandidaten → spart Daten-Credits)
5. Entry im Retest, **struktureller Stop hinter dem Order-Block**, mehrstufige Ziele an der Liquidität
6. Nur Signale mit Confidence ≥ 70 und Chance/Risiko ≥ 1:1.5
7. Sofort-Meldung nach jedem Scan mit Aktivität (kein /today nötig)

## Kosten
- **Scans: 0 €** (rein technisch, nur gratis Twelve-Data-Abrufe)
- **Chat-Fragen**: Sonnet 4.6 (günstig), nur auf deine Frage hin
- **/deep <frage>**: Opus 4.8 Premium-Recherche, nur auf Abruf
- Optional zurück auf KI-Signalanalyse (nur `technical`): Variable `USE_AI_ANALYSIS=true`

## Setup (Railway Variables)
| Variable | Wert |
|---|---|
| TELEGRAM_TOKEN | von BotFather |
| TELEGRAM_CHAT_ID | von /start |
| ANTHROPIC_API_KEY | console.anthropic.com (nur für Chat/Deep) |
| TWELVE_DATA_KEY | twelvedata.com (gratis) |

Optionale Variablen: STRATEGY (orderblock/technical), INTERVAL, HTF_INTERVAL, SIGNAL_COOLDOWN_H,
SCAN_INTERVAL_MIN, PAIRS, CONFIDENCE_MIN, MAX_SIGNALS_DAY, USE_AI_ANALYSIS,
ACCOUNT_SIZE, RISK_MIN_PCT, RISK_MAX_PCT, MAX_LOT, NUM_TPS, CAPITAL_SIZE_FACTOR


## 💹 Trading über Capital.com (Demo)
Optional kannst du Signale per Knopfdruck direkt auf deinem Capital.com-Konto handeln.
**Standardmäßig Demo, und nur auf deinen Button-Druck — niemals automatisch.**

1. In der Capital.com-App/Website: **2FA aktivieren** (Pflicht vor API-Key!)
2. Settings → **API integrations** → **Generate new key**: Label vergeben, ein **eigenes Passwort** für den Key setzen, 2FA-Code eingeben
3. In Railway setzen:
   - `BROKER` = `capital`
   - `CAPITAL_API_KEY` = der generierte Key
   - `CAPITAL_IDENTIFIER` = deine Login-E-Mail
   - `CAPITAL_PASSWORD` = das **Custom-Passwort des API-Keys** (nicht dein Konto-Passwort!)
   - `CAPITAL_ENV` = `demo` (für echtes Geld erst `live`, wenn du sicher bist)
   - `TRADING_ENABLED` = `true`

Dann kommt jedes Signal mit Buttons:
- **✅ Accept** → öffnet pro Take-Profit eine Teil-Order (Size aufgeteilt), alle mit demselben Stop
- **❌ Decline** → verwirft
- **✏️ LOT bearbeiten** → eigene Größe eingeben, dann erscheinen die Buttons erneut

**⚠️ Order-Größe (kein Lot mehr — Capital-Size):** Capital.coms „size" ist eine eigene Einheit, nicht ein MT5-Lot. Der Bot trifft automatisch die **Mindestgröße** (er vergrößert die Order so lange, bis Capital sie akzeptiert) und steigt per **Markt-Order** ein, damit **Stop UND alle Take-Profits** zuverlässig gesetzt werden. Willst du **größer** traden: `CAPITAL_SIZE_FACTOR` hochsetzen (z.B. `5` = 5× Größe) oder die Größe per Button beim Signal anpassen — und in der Capital.com-App Margin/Wert prüfen. Größere Size = größere Gewinne **und Verluste**.

Alternativ OANDA: `BROKER=oanda` + `OANDA_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENV=practice`.
Ohne Trading-Variablen kommen die Signale einfach als Text.

## Befehle
/status — Markt, Session, Analyse-Modus, Trading-Status (Broker + Demo/Live)
/scan — sofort scannen (meldet Ergebnis direkt)
/today — Tagesübersicht
/lot — Positionsgröße aus Risiko berechnen
/deep <frage> — Premium-Recherche über Opus 4.8
/pairs — überwachte Paare
Freitext → Antwort über Sonnet 4.6

## ⚠️ Disclaimer
Kein Finanzrat. Forex-Scalping ist hochriskant. Das Bot-Signal ist rein technisch und
berücksichtigt keine News — prüfe den Wirtschaftskalender selbst. Triff jede Entscheidung
selbst und riskiere pro Trade nur 1-2% deines Kapitals.
