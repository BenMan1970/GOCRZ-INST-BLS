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
        
        # Calcul des DM lissés
        plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        
        # Correction: Suppression de la ligne 'dx' buguée et calcul direct du retour
        # Calcul du DX sans variable intermédiaire inutile pour éviter l'erreur NameError
        di_plus = plus_dm / atr
        di_minus = minus_dm / atr
        
        dx = (abs(di_plus - di_minus) / (di_plus + di_minus)) * 100
        return dx.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

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

        # 2. BIAIS & ZONES
        # CORRECTION: Calcul d'une vraie EMA (ewm) au lieu de la SMA (rolling) pour correspondre au nom 'ema5'
        ema5 = df_d['close'].ewm(span=5, adjust=False).mean().iloc[-1]
        bias = "BULLISH" if price > ema5 else "BEARISH"
        
        zone = "NEUTRAL"
        if bias == "BULLISH" and price < m_open: zone = "DISCOUNT (BUY) 🟢"
        elif bias == "BEARISH" and price > m_open: zone = "PREMIUM (SELL) 🔴"

        # 3. SCORE V10
        score = 0
        if bias == "BULLISH": score += 3
        hma = QuantEngine.hma(df_m15['close'], 20)
        hma_t = "BULLISH" if hma.iloc[-1] > hma.iloc[-2] else "BEARISH"
        if hma_t == bias: score += 4
        if price > df_m15['open'].iloc[-1]: score += 3 
        if df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]: score += 4 

        quality = "💎 A+ SETUP" if score >= 12 else "✅ A SETUP" if score >= 9 else "⚖️ B SETUP" if score >= 7 else "IGNORE"

        return {"Actif": ticker, "Signal": bias, "Zone": zone, "Qualité": quality, "Score": score, "HMA 20": hma_t}
    except Exception as e:
        # Ajout d'un print pour debug dans les logs si nécessaire
        print(f"Erreur analyse {ticker}: {e}")
        return None

def fetch_oanda_data(client, instrument, granularity, count):
    try:
        r = instruments.InstrumentsCandles(instrument=instrument, params={"count": count, "granularity": granularity})
        client.request(r)
        df = pd.DataFrame([{"time": pd.to_datetime(c["time"]), "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]), "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])} for c in r.response.get("candles", []) if c["complete"]])
        if not df.empty:
            df.set_index("time", inplace=True)
            df.index = df.index.tz_localize('UTC') if df.index.tz is None else df.index
        return df
    except:
        return pd.DataFrame()

# ===============================
# INTERFACE
# ===============================

def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner Manuel")

    # CORRECTION: Utilisation du nom de secret exact fourni (OANDA_ACCESS_TOKEN)
    if "OANDA_ACCESS_TOKEN" not in st.secrets:
        st.error("ERREUR : La clé 'OANDA_ACCESS_TOKEN' est introuvable dans vos secrets Streamlit.")
        st.stop()

    # Note: OANDA_ACCOUNT_ID n'est pas nécessaire pour récupérer les bougies (instruments), 
    # mais nécessaire pour passer des ordres. On utilise ici uniquement le token.
    client = oandapyV20.API(access_token=st.secrets["OANDA_ACCESS_TOKEN"], environment="practice")

    assets = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "AUD_USD", "XAU_USD", "NAS100_USD", "GBP_CHF", "CAD_CHF"]

    if st.button("LANCER LE SCANNER 🚀", use_container_width=True):
        results = []
        progress = st.progress(0)
        status_text = st.empty()
        
        for i, ticker in enumerate(assets):
            status_text.text(f"Analyse de {ticker}...")
            res = analyze_asset(client, ticker)
            if res: results.append(res)
            time.sleep(0.1) # Petit délai pour ne pas saturer l'API
            progress.progress((i + 1) / len(assets))

        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)
            
            # Fonction de style améliorée
            def color_cells(val):
                if 'BULLISH' in str(val) or '🟢' in str(val): return 'color: #00ff00'
                elif 'BEARISH' in str(val) or '🔴' in str(val): return 'color: #ff4b4b'
                return 'color: white'

            st.dataframe(df.style.applymap(color_cells, subset=['Signal', 'HMA 20', 'Zone']), use_container_width=True)
            st.success("Scan terminé !")
        else:
            st.warning("Aucun résultat trouvé. Vérifiez votre connexion API ou le marché (ex: week-end).")

if __name__ == "__main__":
    main()
