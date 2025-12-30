# ============================================================
# BLUESTAR ULTIMATE V4 — INSTITUTIONAL FINAL
# Based on V3 | OANDA | Streamlit
# ============================================================

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import logging
from datetime import datetime, timezone
from scipy import stats

# ============================================================
# CONFIGURATION & UI (UNCHANGED)
# ============================================================

st.set_page_config(
    page_title="Bluestar Ultimate V4 Institutional",
    layout="centered",
    page_icon="💎"
)

logging.basicConfig(level=logging.INFO)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700;900&display=swap');
* { font-family: 'Roboto', sans-serif; }
.stApp {
    background-color: #0f1117;
    background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%);
}
.main .block-container { max-width: 950px; padding-top: 2rem; }
h1 {
    background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 900; font-size: 2.8em; text-align: center;
}
</style>
""", unsafe_allow_html=True)

# ============================================================
# SESSION STATE
# ============================================================

if "cache" not in st.session_state:
    st.session_state.cache = {}
if "signal_history" not in st.session_state:
    st.session_state.signal_history = {}
if "cs_cache" not in st.session_state:
    st.session_state.cs_cache = {"data": None, "time": None}

# ============================================================
# OANDA CLIENT
# ============================================================

class OandaClient:
    def __init__(self):
        self.client = oandapyV20.API(
            access_token=st.secrets["OANDA_ACCESS_TOKEN"],
            environment=st.secrets.get("OANDA_ENVIRONMENT", "practice")
        )

    def get_candles(self, instrument, granularity, count):
        key = f"{instrument}_{granularity}"
        if key in st.session_state.cache:
            ts, df = st.session_state.cache[key]
            if (datetime.now() - ts).total_seconds() < 60:
                return df

        params = {"count": count, "granularity": granularity, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)
        self.client.request(r)

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

        df = pd.DataFrame(data)
        if not df.empty:
            st.session_state.cache[key] = (datetime.now(), df)
        return df

# ============================================================
# ASSETS
# ============================================================

ASSETS = [
    "EUR_USD","GBP_USD","USD_JPY","USD_CHF","AUD_USD","USD_CAD","NZD_USD",
    "EUR_GBP","EUR_JPY","EUR_CHF","EUR_CAD","EUR_AUD","EUR_NZD",
    "GBP_JPY","GBP_CHF","GBP_CAD","GBP_AUD","GBP_NZD",
    "AUD_JPY","AUD_CAD","AUD_CHF","AUD_NZD",
    "CAD_JPY","CAD_CHF","NZD_JPY","NZD_CAD","NZD_CHF","CHF_JPY",
    "XAU_USD","US30_USD"
]

# ============================================================
# QUANT CORE (V3 BASE)
# ============================================================

class QuantEngine:

    @staticmethod
    def atr(df, period=14):
        tr = pd.concat([
            df["high"] - df["low"],
            abs(df["high"] - df["close"].shift()),
            abs(df["low"] - df["close"].shift())
        ], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean().iloc[-1]

    @staticmethod
    def rsi(df, period=7):
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        rs = gain.ewm(alpha=1/period, adjust=False).mean() / \
             loss.ewm(alpha=1/period, adjust=False).mean()
        return 100 - (100 / (1 + rs))

    @staticmethod
    def zscore_structure(df, lookback=20):
        if len(df) < lookback:
            return 0
        z = stats.zscore(df["close"].iloc[-lookback:])
        if z[-1] > 1.5: return 1
        if z[-1] < -1.5: return -1
        return 0

# ============================================================
# INSTITUTIONAL ENGINE (V4)
# ============================================================

class InstitutionalEngine:

    @staticmethod
    def adx(df, period=14):
        high, low, close = df["high"], df["low"], df["close"]
        tr = pd.concat([
            high - low,
            abs(high - close.shift()),
            abs(low - close.shift())
        ], axis=1).max(axis=1)

        atr = tr.ewm(span=period, adjust=False).mean()
        up = high.diff()
        down = -low.diff()

        plus_dm = np.where((up > down) & (up > 0), up, 0)
        minus_dm = np.where((down > up) & (down > 0), down, 0)

        plus_di = 100 * pd.Series(plus_dm).ewm(span=period, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus_dm).ewm(span=period, adjust=False).mean() / atr
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)

        return dx.ewm(span=period, adjust=False).mean().iloc[-1]

    @staticmethod
    def market_regime(df_h4):
        adx = InstitutionalEngine.adx(df_h4)
        if adx < 15:
            return "RANGE", 0.0
        if adx < 22:
            return "WEAK_TREND", 0.7
        return "TREND", 1.0

    @staticmethod
    def obv_score(df):
        change = df["close"].diff()
        direction = np.where(change > 0, df["volume"],
                     np.where(change < 0, -df["volume"], 0))
        obv = pd.Series(direction).cumsum()
        ma = obv.rolling(20).mean()
        if len(ma.dropna()) == 0:
            return 1.0
        return 1.15 if obv.iloc[-1] > ma.iloc[-1] else 0.85

# ============================================================
# SIGNAL PROBABILITY V4
# ============================================================

def compute_probability(df_m5, df_h4, df_d, direction):
    atr = QuantEngine.atr(df_m5)
    rsi = QuantEngine.rsi(df_m5)

    rsi_mom = rsi.iloc[-1] - rsi.iloc[-2]

    if direction == "BUY" and not (rsi.iloc[-2] < 50 <= rsi.iloc[-1]):
        return 0
    if direction == "SELL" and not (rsi.iloc[-2] > 50 >= rsi.iloc[-1]):
        return 0

    base_prob = 0.6 + min(abs(rsi_mom) / 10, 0.2)

    z = QuantEngine.zscore_structure(df_h4)
    base_prob *= 1.15 if (direction == "BUY" and z > 0) or (direction == "SELL" and z < 0) else 0.9

    regime, regime_mult = InstitutionalEngine.market_regime(df_h4)
    if regime == "RANGE":
        return 0

    obv_mult = InstitutionalEngine.obv_score(df_m5)

    final_prob = base_prob * regime_mult * obv_mult
    return min(final_prob, 0.98)

# ============================================================
# SCANNER
# ============================================================

def run_scan(api, min_conf=0.75):
    signals = []
    for sym in ASSETS:
        try:
            df_m5 = api.get_candles(sym, "M5", 150)
            df_h4 = api.get_candles(sym, "H4", 100)
            df_d = api.get_candles(sym, "D", 100)

            if df_m5.empty or df_h4.empty or df_d.empty:
                continue

            rsi = QuantEngine.rsi(df_m5)
            direction = None
            if rsi.iloc[-2] < 50 <= rsi.iloc[-1]:
                direction = "BUY"
            elif rsi.iloc[-2] > 50 >= rsi.iloc[-1]:
                direction = "SELL"
            else:
                continue

            prob = compute_probability(df_m5, df_h4, df_d, direction)
            if prob < min_conf:
                continue

            price = df_m5["close"].iloc[-1]
            atr = QuantEngine.atr(df_m5)

            sl = price - atr * 1.5 if direction == "BUY" else price + atr * 1.5
            tp = price + atr * 3.0 if direction == "BUY" else price - atr * 3.0

            signals.append({
                "symbol": sym,
                "type": direction,
                "price": price,
                "prob": prob,
                "time": df_m5["time"].iloc[-1],
                "sl": sl,
                "tp": tp,
                "rr": 2.0
            })

        except Exception as e:
            logging.error(e)

    return sorted(signals, key=lambda x: x["prob"], reverse=True)

# ============================================================
# UI
# ============================================================

def main():
    st.title("💎 BLUESTAR ULTIMATE V4")
    st.caption("Institutional Probability Scanner")

    api = OandaClient()
    min_conf = st.slider("Confiance minimale (%)", 60, 95, 75) / 100

    if st.button("🔍 Scanner le marché"):
        signals = run_scan(api, min_conf)
        if not signals:
            st.warning("Aucun signal institutionnel détecté.")
        for s in signals:
            with st.expander(f"{s['symbol']} | {s['type']} | {int(s['prob']*100)}%"):
                st.write(s)

if __name__ == "__main__":
    main()
