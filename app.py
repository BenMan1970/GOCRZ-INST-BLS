import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timezone
import time

# ===============================
# ENGINE ICT (FVG, PD ARRAYS, HMA)
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
        # Bullish FVG (Gap entre High i-2 et Low i)
        if df['low'].iloc[-1] > df['high'].iloc[-3]: return "BULLISH"
        # Bearish FVG (Gap entre Low i-2 et High i)
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
# LOGIQUE DE SCANNER (14 POINTS)
# ===============================
def analyze_asset(client, ticker):
    # Récupération des données (D, H4, H1, M15)
    df_d = fetch_oanda(client, ticker, "D", 5)
    df_h4 = fetch_oanda(client, ticker, "H4", 20)
    df_h1 = fetch_oanda(client, ticker, "H1", 20)
    df_m15 = fetch_oanda(client, ticker, "M15", 50)

    if df_d.empty or df_h4.empty or df_h1.empty or df_m15.empty: return None

    price = df_m15['close'].iloc[-1]
    score = 0
    confluences = []

    # 1. DAILY BIAS (Filtre Principal)
    ema_d = df_d['close'].rolling(5).mean().iloc[-1]
    bias = "BULLISH" if price > ema_d else "BEARISH"
    score += 3
    confluences.append(f"Bias {bias}")

    # 2. MIDNIGHT OPEN & PD ARRAY (Premium/Discount)
    # On simule le Midnight Open avec la première bougie de la journée
    midnight_open = df_m15.between_time('00:00', '01:00')['open'].iloc[0] if not df_m15.between_time('00:00', '01:00').empty else price
    pdh, pdl = df_d['high'].iloc[-2], df_d['low'].iloc[-2]
    
    zone = "NEUTRAL"
    if bias == "BULLISH" and price < midnight_open: zone = "DISCOUNT 🟢"
    elif bias == "BEARISH" and price > midnight_open: zone = "PREMIUM 🔴"

    # 3. MTF ALIGNMENT (Daily/H4/H1)
    align_count = 0
    if (bias == "BULLISH" and price > df_h4['close'].iloc[-1]): align_count += 1
    if (bias == "BULLISH" and price > df_h1['close'].iloc[-1]): align_count += 1
    if (bias == "BEARISH" and price < df_h4['close'].iloc[-1]): align_count += 1
    if (bias == "BEARISH" and price < df_h1['close'].iloc[-1]): align_count += 1
    
    if align_count >= 2: 
        score += 2
        confluences.append("MTF Aligné")

    # 4. FVG DETECTION (H4/H1)
    fvg_h4 = ICTEngine.detect_fvg(df_h4)
    fvg_h1 = ICTEngine.detect_fvg(df_h1)
    if fvg_h4 == bias: score += 3; confluences.append("FVG H4")
    if fvg_h1 == bias: score += 2; confluences.append("FVG H1")

    # 5. MOMENTUM (HMA 20 & ADX)
    hma20 = ICTEngine.get_hma(df_m15['close'], 20)
    hma_color = "GREEN" if hma20.iloc[-1] > hma20.iloc[-2] else "RED"
    if (bias == "BULLISH" and hma_color == "GREEN") or (bias == "BEARISH" and hma_color == "RED"):
        score += 1; confluences.append("HMA OK")
    
    adx_v = ICTEngine.get_adx(df_h1)
    if adx_v > 20: score += 1; confluences.append("ADX > 20")

    # 6. M15 REBOND
    if (bias == "BULLISH" and df_m15['close'].iloc[-1] > df_m15['open'].iloc[-1]):
        score += 2; confluences.append("Rebond M15")

    # Classification
    quality = "IGNORE"
    if score >= 12: quality = "💎 A+ SETUP"
    elif score >= 9: quality = "✅ A SETUP"
    elif score >= 7: quality = "⚖️ B SETUP"

    return {
        "Actif": ticker, "Biais": bias, "Zone": zone, 
        "Qualité": quality, "Score": score, "ADX": round(adx_v, 1),
        "Confluences": ", ".join(confluences)
    }

# ===============================
# FONCTIONS TECHNIQUES OANDA
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
# INTERFACE STREAMLIT
# ===============================
def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner Manuel")
    
    # Secrets
    try:
        client = oandapyV20.API(access_token=st.secrets["OANDA_ACCESS_TOKEN"], environment="practice")
    except:
        st.error("Configurez OANDA_ACCESS_TOKEN dans les Secrets.")
        st.stop()

    assets = [
        "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "NZD_USD", "USD_CHF",
        "EUR_GBP", "EUR_JPY", "EUR_AUD", "EUR_CAD", "GBP_JPY", "XAU_USD", "US30_USD", 
        "NAS100_USD", "SPX500_USD", "DE30_EUR"
    ]

    if st.button("LANCER LE SCAN GLOBAL 🚀", use_container_width=True):
        results = []
        bar = st.progress(0)
        for i, ticker in enumerate(assets):
            bar.progress((i+1)/len(assets))
            res = analyze_asset(client, ticker)
            if res: results.append(res)
            time.sleep(0.05)

        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)
            
            # Affichage des alertes A+
            top = df[df["Score"] >= 12]
            if not top.empty:
                st.subheader("🔥 Alertes Institutionnelles (A+)")
                for _, row in top.iterrows():
                    st.info(f"**{row['Actif']}** : {row['Zone']} | Score: {row['Score']}/14 | {row['Confluences']}")

            st.subheader("📊 Tableau de Bord")
            st.dataframe(df.style.background_gradient(cmap='RdYlGn', subset=['Score']), use_container_width=True)
        else:
            st.warning("Aucune donnée. Vérifiez l'API.")

if __name__ == "__main__":
    main()
