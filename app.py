import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timezone
import time

# ===============================
# ENGINE ICT & MOMENTUM
# ===============================
class ICTEngine:
    @staticmethod
    def get_hma(series, period=20):
        half, sqrt = int(period / 2), int(np.sqrt(period))
        def wma(s, p):
            weights = np.arange(1, p + 1)
            return s.rolling(p).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)
        return wma(2 * wma(series, half) - wma(series, period), sqrt)

    @staticmethod
    def detect_fvg(df):
        if len(df) < 3: return None
        if df['low'].iloc[-1] > df['high'].iloc[-3]: return "BULLISH"
        if df['high'].iloc[-1] < df['low'].iloc[-3]: return "BEARISH"
        return None

    @staticmethod
    def get_adx(df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        up, down = high.diff(), -low.diff()
        pdm = pd.Series(np.where((up > down) & (up > 0), up, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        mdm = pd.Series(np.where((down > up) & (down > 0), down, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        adx = (abs((pdm/atr) - (mdm/atr)) / ((pdm/atr) + (mdm/atr)) * 100).ewm(alpha=1/period, adjust=False).mean()
        return adx.iloc[-1]

# ===============================
# ANALYSE PAR ACTIF (14 POINTS)
# ===============================
def analyze_asset(client, ticker):
    df_d = fetch_oanda(client, ticker, "D", 5)
    df_h4 = fetch_oanda(client, ticker, "H4", 20)
    df_h1 = fetch_oanda(client, ticker, "H1", 20)
    df_m15 = fetch_oanda(client, ticker, "M15", 50)

    if df_d.empty or df_h4.empty or df_h1.empty or df_m15.empty: return None

    price = df_m15['close'].iloc[-1]
    last_candle_time = df_m15.index[-1]
    score = 0
    
    # 1. DAILY BIAS
    bias = "BULLISH" if price > df_d['close'].iloc[-1] else "BEARISH"
    score += 3

    # 2. MIDNIGHT OPEN (PREMIUM / DISCOUNT)
    # Récupération de l'ouverture de la première bougie M15 de la journée UTC
    today = datetime.now(timezone.utc).date()
    midnight_candles = df_m15[df_m15.index.date == today]
    midnight_open = midnight_candles['open'].iloc[0] if not midnight_candles.empty else df_m15['open'].iloc[0]
    
    zone = "NEUTRAL"
    if bias == "BULLISH" and price < midnight_open: zone = "DISCOUNT (BUY)"
    elif bias == "BEARISH" and price > midnight_open: zone = "PREMIUM (SELL)"

    # 3. MTF ALIGNMENT
    align = 0
    if bias == "BULLISH":
        if price > df_h4['close'].iloc[-1]: align += 1
        if price > df_h1['close'].iloc[-1]: align += 1
    else:
        if price < df_h4['close'].iloc[-1]: align += 1
        if price < df_h1['close'].iloc[-1]: align += 1
    if align >= 2: score += 2

    # 4. FVG (H4/H1)
    if ICTEngine.detect_fvg(df_h4) == bias: score += 3
    if ICTEngine.detect_fvg(df_h1) == bias: score += 2

    # 5. HMA 20 (Momentum M15)
    hma20 = ICTEngine.get_hma(df_m15['close'], 20)
    hma_trend = "BULLISH" if hma20.iloc[-1] > hma20.iloc[-2] else "BEARISH"
    if hma_trend == bias: score += 1

    # 6. ADX & M15 REBOND
    adx_v = ICTEngine.get_adx(df_h1)
    if adx_v > 20: score += 1
    if (bias == "BULLISH" and df_m15['close'].iloc[-1] > df_m15['open'].iloc[-1]) or \
       (bias == "BEARISH" and df_m15['close'].iloc[-1] < df_m15['open'].iloc[-1]):
        score += 2

    # 7. QUALITÉ & FRAÎCHEUR
    quality = "IGNORE"
    if score >= 12: quality = "💎 A+ SETUP"
    elif score >= 9: quality = "✅ A SETUP"
    elif score >= 7: quality = "⚖️ B SETUP"

    diff = datetime.now(timezone.utc) - last_candle_time
    hours, remainder = divmod(int(diff.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    freshness = f"{hours}h {minutes}m"

    return {
        "Actif": ticker,
        "Signal": bias,
        "Zone": zone,
        "Qualité": quality,
        "Score": score,
        "HMA 20": hma_trend,
        "Fraîcheur": freshness
    }

# ===============================
# UTILITAIRES OANDA
# ===============================
def fetch_oanda(client, ticker, granularity, count):
    try:
        r = instruments.InstrumentsCandles(instrument=ticker, params={"count": count, "granularity": granularity})
        client.request(r)
        df = pd.DataFrame([{"time": c["time"], "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]), 
                            "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])} for c in r.response.get("candles", []) if c["complete"]])
        if not df.empty:
            df['time'] = pd.to_datetime(df['time'])
            df.set_index('time', inplace=True)
        return df
    except: return pd.DataFrame()

# ===============================
# STREAMLIT UI
# ===============================
def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner Manuel")

    try:
        token = st.secrets["OANDA_ACCESS_TOKEN"]
        client = oandapyV20.API(access_token=token, environment="practice")
    except:
        st.error("Configurez OANDA_ACCESS_TOKEN dans les Secrets.")
        st.stop()

    # Liste des 33 actifs
    assets = [
        "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "NZD_USD", "USD_CHF",
        "EUR_GBP", "EUR_JPY", "EUR_AUD", "EUR_CAD", "EUR_NZD", "EUR_CHF",
        "GBP_JPY", "GBP_AUD", "GBP_CAD", "GBP_NZD", "GBP_CHF",
        "AUD_JPY", "AUD_CAD", "AUD_NZD", "AUD_CHF",
        "NZD_JPY", "NZD_CAD", "NZD_CHF",
        "CAD_JPY", "CAD_CHF", "CHF_JPY",
        "XAU_USD", "US30_USD", "NAS100_USD", "SPX500_USD", "DE30_EUR"
    ]

    if st.button("LANCER LE SCANNER 🚀", use_container_width=True):
        results = []
        bar = st.progress(0)
        for i, ticker in enumerate(assets):
            bar.progress((i+1)/len(assets))
            res = analyze_asset(client, ticker)
            if res: results.append(res)
            time.sleep(0.05)

        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)

            # Styling des couleurs
            def color_logic(val):
                if val == "BULLISH": return 'color: #00ff00; font-weight: bold'
                if val == "BEARISH": return 'color: #ff4b4b; font-weight: bold'
                return ''

            def highlight_quality(row):
                if "A+" in row["Qualité"]: return ['background-color: #004d00'] * len(row)
                return [''] * len(row)

            st.subheader("📊 Résultats du Scanner")
            st.dataframe(
                df.style.applymap(color_logic, subset=['Signal', 'HMA 20'])
                        .apply(highlight_quality, axis=1)
                        .background_gradient(cmap='RdYlGn', subset=['Score'], vmin=0, vmax=14),
                use_container_width=True,
                height=1000
            )
            st.success(f"Scan terminé à {datetime.now().strftime('%H:%M:%S')} (UTC)")

if __name__ == "__main__":
    main()
