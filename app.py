import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timezone, timedelta
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
        plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        di_plus = plus_dm / atr
        di_minus = minus_dm / atr
        dx = (abs(di_plus - di_minus) / (di_plus + di_minus)) * 100
        return dx.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

# ===============================
# ANALYSE & ICT LOGIC (CORRIGÉ)
# ===============================

def analyze_asset(client, ticker):
    try:
        # 1. Récupération des données
        # Augmenter le count M15 pour bien trouver la bougie de minuit et les PDH/PDL
        df_d = fetch_oanda_data(client, ticker, "D", 10)
        df_m15 = fetch_oanda_data(client, ticker, "M15", 200) 
        
        if df_d.empty or df_m15.empty: return None

        price = df_m15['close'].iloc[-1]
        
        # 2. TIMEZONE SETUP (NEW YORK)
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        
        now_ny = datetime.now(ny_tz)
        today_ny_date = now_ny.date()
        
        # 3. MIDNIGHT OPEN (00:00 NEW YORK)
        # Recherche de la bougie M15 qui débute à 00:00 NY aujourd'hui
        midnight_candle = df_m15[(df_m15.index.date == today_ny_date) & (df_m15.index.hour == 0) & (df_m15.index.minute == 0)]
        
        if midnight_candle.empty:
            # Fallback si minuit n'est pas encore là (ex: dimanche soir ou early monday)
            m_open = df_m15['open'].iloc[0] 
        else:
            m_open = midnight_candle['open'].iloc[0]

        # 4. PDH & PDL (Previous Day High/Low)
        # On prend la journée précédente (hier) par rapport à aujourd'hui en NY time
        yesterday_ny_date = today_ny_date - timedelta(days=1)
        
        # Filtre sur les données d'hier
        df_yesterday = df_m15[df_m15.index.date == yesterday_ny_date]
        
        # Gestion des weekends (si hier est dimanche, on prend vendredi etc...)
        # On recule jusqu'à trouver des données
        while df_yesterday.empty and yesterday_ny_date > today_ny_date - timedelta(days=5):
            yesterday_ny_date -= timedelta(days=1)
            df_yesterday = df_m15[df_m15.index.date == yesterday_ny_date]

        if df_yesterday.empty:
            pdh, pdl = np.nan, np.nan
        else:
            pdh = df_yesterday['high'].max()
            pdl = df_yesterday['low'].min()

        # 5. BIAIS (Tendance Journalière)
        # Calcul EMA 5 sur le Daily (Simple Momentum)
        ema5 = df_d['close'].ewm(span=5, adjust=False).mean().iloc[-1]
        bias = "BULLISH" if price > ema5 else "BEARISH"

        # 6. ZONES PREMIUM / DISCOUNT (LOGIQUE STRICTE ICT)
        # Si Prix > Midnight Open -> PREMIUM (Chercher VENTE)
        # Si Prix < Midnight Open -> DISCOUNT (Chercher ACHAT)
        
        if price > m_open:
            zone = "PREMIUM 🔴" 
        else:
            zone = "DISCOUNT 🟢"
            
        # 7. LOGIQUE DE SCORE V10
        score = 0
        
        # Points pour le Biais
        if bias == "BULLISH": score += 3
        
        # Points pour HMA 20
        hma = QuantEngine.hma(df_m15['close'], 20)
        hma_t = "BULLISH" if hma.iloc[-1] > hma.iloc[-2] else "BEARISH"
        if hma_t == bias: score += 4
        
        # Points Momentum Bougie
        if price > df_m15['open'].iloc[-1]: score += 3 
        
        # Points FVG (Détection simple)
        if df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]: score += 4

        # Qualité du Setup
        quality = "💎 A+ SETUP" if score >= 12 else "✅ A SETUP" if score >= 9 else "⚖️ B SETUP" if score >= 7 else "IGNORE"

        # Calcul distance PDH (optionnel pour affichage)
        dist_pdh = ((pdh - price) / price) * 100 if not np.isnan(pdh) else 0

        return {
            "Actif": ticker, 
            "Signal": bias, 
            "Zone": zone, 
            "Qualité": quality, 
            "Score": score, 
            "HMA 20": hma_t,
            "Midnight": round(m_open, 5),
            "PDH": round(pdh, 5) if not np.isnan(pdh) else 0,
            "PDL": round(pdl, 5) if not np.isnan(pdl) else 0
        }

    except Exception as e:
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
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner ICT")

    if "OANDA_ACCESS_TOKEN" not in st.secrets:
        st.error("ERREUR : La clé 'OANDA_ACCESS_TOKEN' est introuvable.")
        st.stop()

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
            time.sleep(0.1) 
            progress.progress((i + 1) / len(assets))

        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)
            
            def color_cells(val):
                if 'BULLISH' in str(val) or '🟢' in str(val): return 'color: #00ff00'
                elif 'BEARISH' in str(val) or '🔴' in str(val): return 'color: #ff4b4b'
                return 'color: white'

            # Affichage amélioré avec les nouvelles colonnes
            st.dataframe(
                df.style.applymap(color_cells, subset=['Signal', 'HMA 20', 'Zone'])
                .format({"Midnight": "{:.5f}", "PDH": "{:.5f}", "PDL": "{:.5f}"}), 
                use_container_width=True
            )
            st.success("Scan terminé !")
        else:
            st.warning("Aucun résultat.")

if __name__ == "__main__":
    main()
