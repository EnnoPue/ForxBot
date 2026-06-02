# 📊 Forex Scalping Signal Bot (kostenlose Engine)

Schneller Forex-Scalping-Bot für 10 liquide Paare. Signale entstehen aus einer
kostenlosen technischen Engine — kein KI-Aufruf pro Signal, also keine laufenden Kosten.

## Wie es funktioniert
1. Lädt 15min-Kursdaten (Twelve Data) für 10 liquide Paare (Majors + EUR/JPY, GBP/JPY, Gold)
2. Berechnet lokal: EMA(20/50/200), RSI, MACD, ATR, ADX, Bollinger, Swing-Levels
3. Technischer Vorfilter inkl. ADX-Trendstärke-Gate (kein Signal in Seitwärtsphasen)
4. 1h-Multi-Timeframe-Check (nur für Top-Kandidaten → spart Daten-Credits)
5. Technische Engine erzeugt Entry/SL/TP: Pullback-Entry, ATR-Stop (volatilitätsadaptiv,
   kein fester %), Swing-Level- oder 2R-Ziel, Confidence aus Confluence
6. Nur Signale mit Confidence ≥ 70 und Chance/Risiko ≥ 1:1.5
7. Sofort-Meldung nach jedem Scan mit Aktivität (kein /today nötig)

## Kosten
- **Scans: 0 €** (rein technisch, nur gratis Twelve-Data-Abrufe)
- **Chat-Fragen**: Sonnet 4.6 (günstig), nur auf deine Frage hin
- **/deep <frage>**: Opus 4.8 Premium-Recherche, nur auf Abruf
- Optional zurück auf KI-Signalanalyse: Variable `USE_AI_ANALYSIS=true`

## Setup (Railway Variables)
| Variable | Wert |
|---|---|
| TELEGRAM_TOKEN | von BotFather |
| TELEGRAM_CHAT_ID | von /start |
| ANTHROPIC_API_KEY | console.anthropic.com (nur für Chat/Deep) |
| TWELVE_DATA_KEY | twelvedata.com (gratis) |

Optionale Variablen: PAIRS, INTERVAL, CONFIDENCE_MIN, MAX_SIGNALS_DAY, USE_AI_ANALYSIS

## Befehle
/status — Markt, Session, Schwellen
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
