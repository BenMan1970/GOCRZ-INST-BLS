import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timedelta
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
        # Lissage EMA 5 final comme dans le script Pine
        return hma.ewm(span=5, adjust=False).mean()

# ===============================
# ANALYSE & ICT LOGIC (STRICT TRIGGER)
# ===============================

def get_trend_bias(df_daily):
    """Détermine le Biais Institutionnel (Daily)"""
    if len(df_daily) < 60: return "NEUTRAL"
    
    close = df_daily['close']
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    
    c = close.iloc[-1]
    e21 = ema21.iloc[-1]
    e50 = ema50.iloc[-1]
    
    # Structure Haussière : Prix > EMA21 > EMA50
    if c > e21 and e21 > e50:
        return "BULLISH"
    # Structure Baissière : Prix < EMA21 < EMA50
    elif c < e21 and e21 < e50:
        return "BEARISH"
    return "NEUTRAL"

def analyze_asset(client, ticker):
    try:
        # 1. Récupération Données
        df_d = fetch_oanda_data(client, ticker, "D", 100)
        # M15 : Besoin de assez de données pour HMA et FVG
        df_m15 = fetch_oanda_data(client, ticker, "M15", 300) 
        
        if df_d.empty or df_m15.empty: return None

        price = df_m15['close'].iloc[-1]
        
        # 2. BIAIS (Daily)
        bias = get_trend_bias(df_d)
        
        # 3. ZONE (Premium/Discount vs Midnight Open)
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        
        today_ny_date = datetime.now(ny_tz).date()
        # Recherche Midnight Open (00:00 NY)
        midnight_mask = (df_m15.index.date == today_ny_date) & (df_m15.index.hour == 0) & (df_m15.index.minute == 0)
        midnight_candles = df_m15[midnight_mask]
        
        if not midnight_candles.empty:
            m_open = midnight_candles['open'].iloc[0]
        else:
            # Fallback si minuit pas encore là
            m_open = df_m15['open'].iloc[0]

        # Définition Zone stricte
        if price > m_open:
            zone = "PREMIUM"
        else:
            zone = "DISCOUNT"

        # 4. LOGIQUE DE TRIGGER (HMA 20 + FVG)
        
        # A. Calcul HMA 20
        hma = QuantEngine.hma(df_m15['close'], 20)
        hma_val = hma.iloc[-1]
        hma_prev = hma.iloc[-2]
        
        # Le Trigger : Changement de couleur
        # HMA monte (Bullish) = HMA actuel > HMA précédent
        # HMA descend (Bearish) = HMA actuel < HMA précédent
        hma_turning_bull = hma_val > hma_prev
        hma_turning_bear = hma_val < hma_prev
        
        # B. Détection FVG
        # FVG Bullish : Low[0] > High[2] (Gap entre bougie -1 et -3)
        fvg_bull = df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]
        # FVG Bearish : High[0] < Low[2]
        fvg_bear = df_m15['high'].iloc[-1] < df_m15['low'].iloc[-3]

        # 5. VALIDATION DU SETUP (A+ SETUP)
        
        setup_valid = False
        signal_type = "NEUTRAL"
        
        # SCENARIO LONG
        # Biais BULLISH + Zone DISCOUNT + HMA vire au VERT + FVG BULLISH
        if bias == "BULLISH" and zone == "DISCOUNT" and hma_turning_bull and fvg_bull:
            setup_valid = True
            signal_type = "BULLISH"
            
        # SCENARIO SHORT
        # Biais BEARISH + Zone PREMIUM + HMA vire au ROUGE + FVG BEARISH
        elif bias == "BEARISH" and zone == "PREMIUM" and hma_turning_bear and fvg_bear:
            setup_valid = True
            signal_type = "BEARISH"

        # 6. FORMATAGE RESULTAT
        if setup_valid:
            quality = "💎 A+ SETUP"
        else:
            # On retourne quand même l'info pour le tableau, mais marqué IGNORE
            quality = "IGNORE"
            # On met le signal actuel juste pour info (ex: Biais mais pas de trigger)
            signal_type = bias 

        # Calcul PDH/PDL pour affichage
        pdh = df_d['high'].iloc[-2] if len(df_d) >= 2 else 0
        pdl = df_d['low'].iloc[-2] if len(df_d) >= 2 else 0

        return {
            "Actif": ticker, 
            "Signal": signal_type, 
            "Zone": zone, 
            "Qualité": quality, 
            "Trigger HMA": "✅" if (hma_turning_bull and signal_type=="BULLISH") or (hma_turning_bear and signal_type=="BEARISH") else "❌",
            "FVG": "✅" if (fvg_bull and signal_type=="BULLISH") or (fvg_bear and signal_type=="BEARISH") else "❌",
            "Midnight": round(m_open, 5),
            "Prix": round(price, 5)
        }

    except Exception as e:
        print(f"Erreur analyse {ticker}: {e}")
        return None

def fetch_oanda_data(client, instrument, granularity, count):
    # (Identique aux versions précédentes, gestion erreur incluse)
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
    st.title("🎯 BLUESTAR SNIPER V10 - Trigger Scanner")

    if "OANDA_ACCESS_TOKEN" not in st.secrets:
        st.error("ERREUR : Clé API manquante.")
        st.stop()

    client = oandapyV20.API(access_token=st.secrets["OANDA_ACCESS_TOKEN"], environment="practice")

    assets = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "AUD_USD", "XAU_USD", "NAS100_USD", "GBP_CHF", "CAD_CHF"]

    if st.button("LANCER LE SCANNER 🚀", use_container_width=True):
        results = []
        progress = st.progress(0)
        
        with st.spinner("Recherche de A+ Setups (HMA Trigger + FVG)..."):
            for i, ticker in enumerate(assets):
                res = analyze_asset(client, ticker)
                if res: results.append(res)
                time.sleep(0.2)
                progress.progress((i + 1) / len(assets))

        if results:
            df = pd.DataFrame(results)
            
            # Affichage des A+ en premier
            df = df.sort_values(by="Qualité", key=lambda x: x == "💎 A+ SETUP", ascending=False)

            def highlight_setup(val):
                if "A+ SETUP" in str(val): return 'background-color: #006400; color: white'
                return ''

            st.dataframe(
                df.style.applymap(highlight_setup, subset=['Qualité'])
                .applymap(lambda x: 'color: #00ff00' if '✅' in str(x) else 'color: gray', subset=['Trigger HMA', 'FVG'])
                .applymap(lambda x: 'color: #00ff00' if 'BULLISH' in str(x) else 'color: #ff4b4b' if 'BEARISH' in str(x) else '', subset=['Signal']),
                use_container_width=True
            )
            
            # Compteur de setups valides
            valid_count = len(df[df['Qualité'] == "💎 A+ SETUP"])
            if valid_count > 0:
                st.success(f"🎯 {valid_count} Setup(s) A+ détecté(s) !")
            else:
                st.info("Aucun setup A+ détecté pour l'instant (Conditions HMA/FVG non remplies).")
        else:
            st.warning("Erreur de connexion aux données.")

if __name__ == "__main__":
    main()
