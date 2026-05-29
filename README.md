# 📊 Forex Signal Bot (Opus 4.8)

Hochselektiver Forex-Daytrading-Bot für die 7 Major-Paare. Schickt max. 2-3 Signale/Tag
— aber nur solche mit hoher Confidence. Stop-Loss und Take-Profit werden aus echter
Marktstruktur (ATR + Swing-Levels) berechnet und von Opus 4.8 mit Live-Recherche begründet.

## Wie es funktioniert
1. Lädt 1h-Kursdaten (Twelve Data) für EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD, USD/CAD, NZD/USD
2. Berechnet lokal Indikatoren: EMA(20/50/200), RSI, MACD, ATR, Bollinger, Swing-Levels
3. Technischer Vorab-Filter → nur starke Setups gehen weiter
4. Opus 4.8 recherchiert live: Fundamental (Notenbanken, News, Kalender) + Sentiment (COT, Retail)
5. Opus bestimmt Entry/SL/TP aus echten Levels und vergibt eine Confidence
6. Nur Signale mit Confidence ≥ 75 und Chance/Risiko ≥ 1:1.5 werden gesendet

## Setup
### 1. Neuer Telegram Bot
@BotFather → /newbot → Token. Bot öffnen → /start → Chat-ID merken.
### 2. Anthropic API Key
console.anthropic.com → API Keys.
### 3. Twelve Data API Key (kostenlos)
twelvedata.com → Sign up → API Key kopieren (Free: 800 Calls/Tag, reicht).
### 4. Railway Environment Variables
| Variable | Wert |
|---|---|
| TELEGRAM_TOKEN | von BotFather |
| TELEGRAM_CHAT_ID | von /start |
| ANTHROPIC_API_KEY | console.anthropic.com |
| TWELVE_DATA_KEY | twelvedata.com |
| AI_MODEL | claude-opus-4-8 (optional) |

## Befehle
/status — Tageszähler & Marktstatus
/scan — sofort scannen (nicht aufs Intervall warten)
/pairs — überwachte Paare
/help — Erklärung
Freitext → Opus 4.8 beantwortet Marktfragen mit Live-Recherche

## ⚠️ Disclaimer
Kein Finanzrat. Forex-Trading ist hochriskant. Signale sind gut recherchierte Vorschläge,
keine Garantie. Triff jede Entscheidung selbst und nutze striktes Risikomanagement.
