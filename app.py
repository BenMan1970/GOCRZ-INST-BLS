# =============================================================
# BlueStar GOCRZ Sniper – Scanner v8.1 Intraday Pro
# CORRECTED VERSION – NO REGRESSION
# UI / COLORS / DISPOSITION: SAME AS ORIGINAL v7
# ONLY LOGIC REFACTORED + CACHE BUG FIXED
# =============================================================

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import warnings
warnings.filterwarnings("ignore")

# -------------------------------------------------------------
# STREAMLIT CONFIG (UNCHANGED)
# -------------------------------------------------------------
st.set_page_config(
    page_title="BlueStar GOCRZ Sniper",
    page_icon="⭐",
    layout="centered"
)

# -------------------------------------------------------------
# DATA FETCH – CACHE SAFE (FIXED)
# -------------------------------------------------------------
@st.cache_data(ttl=300)
def fetch_candles(instrument, granularity, count):
    api = oandapyV20.API(access_token=st.secrets["OANDA_ACCESS_TOKEN"])
    params = {"count": count, "granularity": granularity, "price": "M"}
    r = instruments.InstrumentsCandles(instrument=instrument, params=params)
    api.request(r)

    data = []
    for c in r.response["candles"]:
        if c["complete"]:
            data.append({
                "time": pd.to_datetime(c["time"]),
                "open": float(c["mid"]["o"]),
                "high": float(c["mid"]["h"]),
                "low": float(c["mid"]["l"]),
                "close": float(c["mid"]["c"]),
                "volume": int(c["volume"])
            })
    return pd.DataFrame(data)

# -------------------------------------------------------------
# INDICATORS (IDENTICAL QUALITY)
# -------------------------------------------------------------
class IND:
    @staticmethod
    def atr(df, p=14):
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1/p, adjust=False).mean().iloc[-1]

    @staticmethod
    def rsi(series, p=14):
        d = series.diff()
        g = d.where(d > 0, 0)
        l = -d.where(d < 0, 0)
        rs = g.ewm(alpha=1/p, adjust=False).mean() / l.ewm(alpha=1/p, adjust=False).mean()
        return 100 - (100 / (1 + rs))

    @staticmethod
    def adx(df, p=14):
        h, l, c = df["high"], df["low"], df["close"]
        up = h.diff()
        dn = -l.diff()
        plus = np.where((up > dn) & (up > 0), up, 0)
        minus = np.where((dn > up) & (dn > 0), dn, 0)
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/p, adjust=False).mean()
        plus_di = 100 * pd.Series(plus).ewm(alpha=1/p, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus).ewm(alpha=1/p, adjust=False).mean() / atr
        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
        return dx.ewm(alpha=1/p, adjust=False).mean().iloc[-1]

    @staticmethod
    def hma(series, p):
        half = int(p / 2)
        sqrt = int(np.sqrt(p))
        wma = lambda s, l: s.rolling(l).apply(lambda x: np.dot(x, np.arange(1, l+1)) / np.arange(1, l+1).sum(), raw=True)
        return wma(2 * wma(series, half) - wma(series, p), sqrt)

# -------------------------------------------------------------
# MARKET STRUCTURE (UNCHANGED PHILOSOPHY)
# -------------------------------------------------------------
def structure_bias(df, side):
    if side == "BUY":
        return df["high"].iloc[-5:].max() > df["high"].iloc[-20:-5].max()
    return df["low"].iloc[-5:].min() < df["low"].iloc[-20:-5].min()

# -------------------------------------------------------------
# INSTITUTIONAL CONTEXT (SOFT ONLY)
# -------------------------------------------------------------
def order_block(df, atr, side):
    for i in range(-25, -3):
        c = df.iloc[i]
        n = df.iloc[i+1]
        body = abs(c["close"] - c["open"])
        if body < atr * 0.2:
            continue
        if side == "BUY" and c["close"] < c["open"] and n["close"] > n["open"]:
            return True
        if side == "SELL" and c["close"] > c["open"] and n["close"] < n["open"]:
            return True
    return False


def fair_value_gap(df, atr, side):
    for i in range(-20, -2):
        a, b, c = df.iloc[i-2], df.iloc[i-1], df.iloc[i]
        if side == "BUY" and c["low"] - a["high"] > atr * 0.2:
            return True
        if side == "SELL" and a["low"] - c["high"] > atr * 0.2:
            return True
    return False

# -------------------------------------------------------------
# SCANNER CORE v8.1 (LOGIC ONLY)
# -------------------------------------------------------------
def scan(df_m5, df_m15, df_h1, symbol, side):
    score = 0
    notes = []

    price = df_m5["close"].iloc[-1]
    atr = IND.atr(df_m5)

    # ---- HARD FILTERS (MAX 3) ----
    if not structure_bias(df_h1, side):
        return None
    score += 20; notes.append("H1 Structure")

    if IND.adx(df_h1) < 15:
        return None
    score += 15; notes.append("H1 Momentum")

    hma20 = IND.hma(df_m5["close"], 20).iloc[-1]
    if side == "BUY" and price > hma20 + atr * 0.5:
        return None
    if side == "SELL" and price < hma20 - atr * 0.5:
        return None
    score += 15; notes.append("Pullback Zone")

    # ---- SOFT CONFLUENCE ----
    rsi_h1 = IND.rsi(df_h1["close"]).iloc[-1]
    if side == "BUY" and rsi_h1 > 55:
        score += 10; notes.append("RSI H1 Bullish")
    if side == "SELL" and rsi_h1 < 45:
        score += 10; notes.append("RSI H1 Bearish")

    if IND.adx(df_m5) > 18:
        score += 10; notes.append("M5 Momentum")

    rsi_m5 = IND.rsi(df_m5["close"], 10).iloc[-1]
    if side == "BUY" and rsi_m5 > 50:
        score += 10; notes.append("RSI Break")
    if side == "SELL" and rsi_m5 < 50:
        score += 10; notes.append("RSI Break")

    if order_block(df_m5, atr, side):
        score += 8; notes.append("Order Block")

    if fair_value_gap(df_m5, atr, side):
        score += 6; notes.append("FVG")

    quality = "WATCHLIST"
    if score >= 80: quality = "ELITE"
    elif score >= 65: quality = "PREMIUM"

    return {
        "symbol": symbol,
        "side": side,
        "score": score,
        "quality": quality,
        "price": price,
        "notes": notes
    }

# -------------------------------------------------------------
# UI – SAME FLOW / SAME COLORS LOGIC
# -------------------------------------------------------------
def main():
    st.title("⭐ BlueStar GOCRZ Sniper")
    st.caption("Intraday Institutional Scanner – Manual Validation Required")

    if st.button("SCAN MARKET"):
        assets = ["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"]
        results = []

        for s in assets:
            df_m5 = fetch_candles(s, "M5", 300)
            df_m15 = fetch_candles(s, "M15", 200)
            df_h1 = fetch_candles(s, "H1", 300)

            if df_m5.empty or df_m15.empty or df_h1.empty:
                continue

            for side in ["BUY", "SELL"]:
                r = scan(df_m5, df_m15, df_h1, s, side)
                if r:
                    results.append(r)

        if not results:
            st.warning("No valid opportunities")
            return

        for r in sorted(results, key=lambda x: x["score"], reverse=True):
            if r["quality"] == "ELITE":
                st.success(f"{r['symbol']} {r['side']} | {r['quality']} | {r['score']}")
            elif r["quality"] == "PREMIUM":
                st.info(f"{r['symbol']} {r['side']} | {r['quality']} | {r['score']}")
            else:
                st.write(f"{r['symbol']} {r['side']} | {r['quality']} | {r['score']}")
            st.caption(" · ".join(r["notes"]))


if __name__ == "__main__":
    main()
