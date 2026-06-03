"""Technische Indikatoren — lokal berechnet aus OHLC (spart API-Calls)."""
import pandas as pd
import numpy as np


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(closes: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(closes, fast) - ema(closes, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def bollinger(closes: pd.Series, period=20, mult=2):
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    return mid + mult * std, mid, mid - mult * std


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — misst Trendstärke (nicht Richtung)."""
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)).astype(float) * up
    minus_dm = ((down > up) & (down > 0)).astype(float) * down
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False).mean()


def swing_levels(df: pd.DataFrame, lookback: int = 40):
    """Jüngstes Swing-Hoch und -Tief (Support/Resistance-Kandidaten)."""
    recent = df.tail(lookback)
    return float(recent["high"].max()), float(recent["low"].min())


def analyze(df: pd.DataFrame) -> dict:
    """Berechnet alle Indikatoren und gibt die letzten Werte zurück."""
    closes = df["close"]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes)
    macd_line, sig_line, hist = macd(closes)
    a = atr(df)
    adx_series = adx(df)
    bb_up, bb_mid, bb_lo = bollinger(closes)
    swing_high, swing_low = swing_levels(df)

    price = float(closes.iloc[-1])

    # Trend bestimmen
    last_e20, last_e50, last_e200 = e20.iloc[-1], e50.iloc[-1], e200.iloc[-1]
    if last_e20 > last_e50 > last_e200:
        trend = "bullish"
    elif last_e20 < last_e50 < last_e200:
        trend = "bearish"
    else:
        trend = "neutral"

    return {
        "price": price,
        "ema20": float(last_e20), "ema50": float(last_e50), "ema200": float(last_e200),
        "rsi": float(r.iloc[-1]),
        "macd": float(macd_line.iloc[-1]), "macd_signal": float(sig_line.iloc[-1]),
        "macd_hist": float(hist.iloc[-1]), "macd_hist_prev": float(hist.iloc[-2]),
        "atr": float(a.iloc[-1]),
        "adx": float(adx_series.iloc[-1]),
        "bb_upper": float(bb_up.iloc[-1]), "bb_lower": float(bb_lo.iloc[-1]),
        "swing_high": swing_high, "swing_low": swing_low,
        "trend": trend,
    }


def prescreen_score(ind: dict) -> tuple[int, str]:
    """
    Schneller technischer Vorab-Filter (0-100) + Richtung.
    ADX dient als Trendstärke-Gate: in Seitwärtsphasen (Chop) kein Signal.
    Nur Setups über Schwelle gehen an die teure KI-Analyse.
    """
    price, rsi_v, trend = ind["price"], ind["rsi"], ind["trend"]
    hist, hist_prev = ind["macd_hist"], ind["macd_hist_prev"]
    adx_v = ind.get("adx", 0)

    # Trendstärke-Gate: unter 14 = kein klarer Trend → ablehnen (für 15min-Scalping)
    if adx_v < 14 or trend == "neutral":
        return 0, "none"

    score = 0
    direction = "none"

    if trend == "bullish":
        direction = "long"
        score += 30
        if 40 <= rsi_v <= 68: score += 20          # Momentum, nicht überkauft
        if hist > 0 and hist > hist_prev: score += 20  # MACD dreht hoch
        if price <= ind["ema20"] * 1.002: score += 15  # Pullback nahe EMA20 = guter Entry
    elif trend == "bearish":
        direction = "short"
        score += 30
        if 32 <= rsi_v <= 60: score += 20
        if hist < 0 and hist < hist_prev: score += 20
        if price >= ind["ema20"] * 0.998: score += 15

    # ADX-Trendstärke-Bonus
    if adx_v >= 30:   score += 15
    elif adx_v >= 23: score += 10
    elif adx_v >= 18: score += 5

    return min(score, 100), direction


def detect_order_block(df, atr):
    """
    Order-Block-/Break-of-Structure-Erkennung (Smart-Money-Stil), regelbasiert.

    Idee (bullisch): starker Impuls macht ein jüngstes Swing-Hoch (Struktur gebrochen),
    danach zieht der Preis in den Order-Block zurück = letzte bärische Kerze vor dem Impuls.
    Einstieg im Retest der Zone. Bärisch spiegelbildlich.

    Gibt dict(direction, ob_low, ob_high, broke_level, price) oder None.
    """
    if df is None or len(df) < 40 or atr <= 0:
        return None
    window = 30
    o = df["open"].to_numpy()[-window:]
    h = df["high"].to_numpy()[-window:]
    l = df["low"].to_numpy()[-window:]
    c = df["close"].to_numpy()[-window:]
    n = len(c); price = float(c[-1])
    recent = 3          # mind. so viele Kerzen Pullback nach dem Impuls-Hoch/Tief

    # ── Bullisch: jüngstes Swing-Hoch (jünger als das Tief), danach Pullback in den Order-Block ──
    i_high = int(np.argmax(h))
    i_low = int(np.argmin(l))
    if 3 <= i_high <= n - 1 - recent and i_high > i_low:
        idx = None
        for i in range(i_high - 1, max(i_high - 8, 0) - 1, -1):
            if c[i] < o[i]:                       # letzte bärische Kerze vor dem Impuls
                idx = i; break
        if idx is not None:
            ob_low, ob_high = float(l[idx]), float(h[idx])
            swing_high = float(h[i_high])
            impulse = swing_high - ob_low
            retr = swing_high - price
            if (impulse >= 2.0 * atr                       # echter Impuls (kein Rauschen)
                    and ob_low <= price <= ob_high * 1.0010  # Preis im OB-Retest
                    and price > ob_low                        # Order-Block noch intakt
                    and retr >= 0.4 * impulse):               # echte Korrektur
                return {"direction": "long", "ob_low": ob_low, "ob_high": ob_high,
                        "broke_level": swing_high, "price": price}

    # ── Bärisch: jüngstes Swing-Tief (jünger als das Hoch), danach Pullback in den Order-Block ──
    if 3 <= i_low <= n - 1 - recent and i_low > i_high:
        idx = None
        for i in range(i_low - 1, max(i_low - 8, 0) - 1, -1):
            if c[i] > o[i]:                       # letzte bullische Kerze vor dem Impuls
                idx = i; break
        if idx is not None:
            ob_low, ob_high = float(l[idx]), float(h[idx])
            swing_low = float(l[i_low])
            impulse = ob_high - swing_low
            retr = price - swing_low
            if (impulse >= 2.0 * atr
                    and ob_low * 0.9990 <= price <= ob_high
                    and price < ob_high
                    and retr >= 0.4 * impulse):
                return {"direction": "short", "ob_low": ob_low, "ob_high": ob_high,
                        "broke_level": swing_low, "price": price}

    return None
