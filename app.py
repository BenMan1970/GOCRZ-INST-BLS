import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timezone
import pytz
import time

# ===============================
# BLUESTAR SNIPER V10 ENGINE
# ===============================

class QuantEngine:
    @staticmethod
    def wma(series, period):
        weights = np.arange(1, period + 1)
        return series.rolling(period).apply(lambda prices: np.dot(prices, weights) / weights.sum(), raw=True)

    @staticmethod
    def hma(series, period=20):
        # Logique HMA 20 + EMA 5 du script TradingView
        half = int(period / 2)
        sqrt = int(np.sqrt(period))
        wma1 = QuantEngine.wma(series, half)
        wma2 = QuantEngine.wma(series, period)
        raw_hma = 2 * wma1 - wma2
        hma = QuantEngine.wma(raw_hma, sqrt)
        return hma.ewm(span=5, adjust=False).mean()

    @staticmethod
    def adx_wilder(df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        up, down = high.diff(), -low.diff()
        plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if 'plus_di' in locals() else 0 # Simplifié pour stabilité
        return (abs((plus_dm/atr) - (minus_dm/atr)) / ((plus_dm/atr) + (minus_dm/atr)) * 100).ewm(alpha=1/period, adjust=False).mean().iloc[-1]

# ===============================
# ANALYSE & ICT LOGIC
# ===============================

def analyze_asset(client, ticker):
    try:
        # Récupération OANDA
        df_d = fetch_oanda_data(client, ticker, "D", 10)
        df_m15 = fetch_oanda_data(client, ticker, "M15", 100)
        if df_d.empty or df_m15.empty: return None

        price = df_m15['close'].iloc[-1]
        
        # 1. MIDNIGHT OPEN (NY TIME)
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        today_ny = datetime.now(ny_tz).date()
        m15_today = df_m15[df_m15.index.date == today_ny]
        m_open = m15_today['open'].iloc[0] if not m15_today.empty else df_m15['open'].iloc[0]

        # 2. BIAIS & ZONES (Correction CADCHF Premium/Discount)
        ema5 = df_d['close'].rolling(5).mean().iloc[-1]
        bias = "BULLISH" if price > ema5 else "BEARISH"
        
        # Discount = SOUS le Midnight Open pour un achat
        zone = "NEUTRAL"
        if bias == "BULLISH" and price < m_open: zone = "DISCOUNT (BUY) 🟢"
        elif bias == "BEARISH" and price > m_open: zone = "PREMIUM (SELL) 🔴"

        # 3. SCORE V10
        score = 0
        if bias == "BULLISH": score += 3
        hma = QuantEngine.hma(df_m15['close'], 20)
        hma_t = "BULLISH" if hma.iloc[-1] > hma.iloc[-2] else "BEARISH"
        if hma_t == bias: score += 4
        if price > df_m15['open'].iloc[-1]: score += 3 # Momentum bougie
        if df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]: score += 4 # FVG

        quality = "💎 A+ SETUP" if score >= 12 else "✅ A SETUP" if score >= 9 else "⚖️ B SETUP" if score >= 7 else "IGNORE"

        return {"Actif": ticker, "Signal": bias, "Zone": zone, "Qualité": quality, "Score": score, "HMA 20": hma_t}
    except: return None

def fetch_oanda_data(client, instrument, granularity, count):
    r = instruments.InstrumentsCandles(instrument=instrument, params={"count": count, "granularity": granularity})
    client.request(r)
    df = pd.DataFrame([{"time": pd.to_datetime(c["time"]), "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]), "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])} for c in r.response.get("candles", []) if c["complete"]])
    if not df.empty:
        df.set_index("time", inplace=True)
        df.index = df.index.tz_localize('UTC') if df.index.tz is None else df.index
    return df

# ===============================
# INTERFACE (VOTRE FORMAT)
# ===============================

def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner Manuel")

    # REPRISE DE VOTRE VARIABLE EXACTE : OANDA_TOKEN
    if "OANDA_TOKEN" not in st.secrets:
        st.error("ERREUR : La clé 'OANDA_TOKEN' est introuvable. Vérifiez vos secrets Streamlit.")
        st.stop()

    client = oandapyV20.API(access_token=st.secrets["OANDA_TOKEN"], environment="practice")

    assets = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "AUD_USD", "XAU_USD", "NAS100_USD", "GBP_CHF", "CAD_CHF"]

    if st.button("LANCER LE SCANNER 🚀", use_container_width=True):
        results = []
        progress = st.progress(0)
        for i, ticker in enumerate(assets):
            res = analyze_asset(client, ticker)
            if res: results.append(res)
            time.sleep(0.1)
            progress.progress((i + 1) / len(assets))

        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)
            st.dataframe(df.style.applymap(lambda x: f"color: {'#00ff00' if 'BULLISH' in str(x) or '🟢' in str(x) else '#ff4b4b'}", subset=['Signal', 'HMA 20', 'Zone']), use_container_width=True)

if __name__ == "__main__":
    main()
