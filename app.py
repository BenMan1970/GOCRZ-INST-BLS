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
        return hma.ewm(span=5, adjust=False).mean()

# ===============================
# ANALYSE & ICT LOGIC (AUDITED)
# ===============================

def get_institutional_bias(df_daily):
    """
    Réplique exacte de la logique 'getInstitutionalTrend' du script TradingView pour le Timeframe Daily.
    Retourne: "BULLISH", "BEARISH" ou "NEUTRAL"
    """
    if len(df_daily) < 200: 
        return "NEUTRAL"
    
    close = df_daily['close']
    
    # Calcul des moyennes (TradingView Script Lignes 151-153)
    sma200 = close.rolling(200).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    
    # Dernières valeurs
    c = close.iloc[-1]
    s200 = sma200.iloc[-1]
    e50 = ema50.iloc[-1]
    e21 = ema21.iloc[-1]
    
    # Logique Perfect/Strong Bull/Bear (Lignes 157-162 du script Pine)
    aboveSMA200 = c > s200
    belowSMA200 = c < s200
    ema50AboveSMA = e50 > s200
    ema50BelowSMA = e50 < s200
    ema21Above50 = e21 > e50
    ema21Below50 = e21 < e50
    
    perfectBull = aboveSMA200 and ema50AboveSMA and ema21Above50 and c > e21
    perfectBear = belowSMA200 and ema50BelowSMA and ema21Below50 and c < e21
    
    strongBull = aboveSMA200 and ema50AboveSMA
    strongBear = belowSMA200 and ema50BelowSMA
    
    if perfectBull or strongBull:
        return "BULLISH"
    elif perfectBear or strongBear:
        return "BEARISH"
    else:
        return "NEUTRAL"

def analyze_asset(client, ticker):
    try:
        # 1. Récupération Données
        # Daily : Besoin de 200 bougies pour la SMA200
        df_d = fetch_oanda_data(client, ticker, "D", 250)
        # M15 : Pour le contexte intraday, Midnight Open, FVG
        df_m15 = fetch_oanda_data(client, ticker, "M15", 200)
        
        if df_d.empty or df_m15.empty: return None

        price = df_m15['close'].iloc[-1]
        
        # 2. BIAIS INSTITUTIONNEL (Basé sur Daily TF)
        # Utilise la nouvelle fonction 'get_institutional_bias'
        bias = get_institutional_bias(df_d)
        
        # 3. TIMEZONE & MIDNIGHT OPEN (NEW YORK)
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        
        now_ny = datetime.now(ny_tz)
        today_ny_date = now_ny.date()
        
        # Trouver l'ouverture de minuit (00:00 NY)
        midnight_mask = (df_m15.index.date == today_ny_date) & (df_m15.index.hour == 0) & (df_m15.index.minute == 0)
        midnight_candles = df_m15[midnight_mask]
        
        if not midnight_candles.empty:
            m_open = midnight_candles['open'].iloc[0]
        else:
            # Fallback : Ouverture du jour actuel si 00:00 pas encore disponible (ex: early session)
            # On prend l'open de la première bougie dispo du jour
            today_candles = df_m15[df_m15.index.date == today_ny_date]
            m_open = today_candles['open'].iloc[0] if not today_candles.empty else df_m15['open'].iloc[-1]

        # 4. PREVIOUS DAY HIGH/LOW (PDH/PDL)
        # Méthode fiable : Prendre la bougie Daily d'hier (iloc[-2])
        # Note : OANDA aligne le Daily sur 17h NY (ou 16h DST), mais le High/Low couvre la session précédente.
        # On suppose que la bougie Daily complétée la plus récente est la référence.
        if len(df_d) >= 2:
            pdh = df_d['high'].iloc[-2]
            pdl = df_d['low'].iloc[-2]
        else:
            pdh, pdl = np.nan, np.nan

        # 5. ZONES PREMIUM / DISCOUNT
        # Règle stricte ICT :
        # Prix > Midnight Open = PREMIUM (Zone de Vente)
        # Prix < Midnight Open = DISCOUNT (Zone d'Achat)
        
        if price > m_open:
            zone = "PREMIUM 🔴" 
        else:
            zone = "DISCOUNT 🟢"
            
        # 6. LOGIQUE DE SCORE V10
        score = 0
        
        # A. Alignement Tendance
        if bias == "BULLISH": score += 4 # Poids augmenté pour la tendance institutionnelle
        elif bias == "BEARISH": score += 0 # Pas de points si bearish (base 0)
        
        # B. HMA 20 (Momentum Court Terme)
        hma = QuantEngine.hma(df_m15['close'], 20)
        hma_t = "BULLISH" if hma.iloc[-1] > hma.iloc[-2] else "BEARISH"
        
        # Si HMA aligné avec le Biais (ou neutre)
        if bias == "BULLISH" and hma_t == "BULLISH": score += 3
        elif bias == "BEARISH" and hma_t == "BEARISH": score += 3
        
        # C. FVG (Fair Value Gap)
        # Détection simple : Low actuel > High d'il y a 2 bougies
        last_fvg_bull = df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]
        last_fvg_bear = df_m15['high'].iloc[-1] < df_m15['low'].iloc[-3]
        
        if bias == "BULLISH" and last_fvg_bull: score += 3
        if bias == "BEARISH" and last_fvg_bear: score += 3

        # D. Position Ideal (Proximité PDH/PDL)
        # Bonus si on est proche des liquidités cibles
        if bias == "BEARISH" and price >= (pdl * 1.001): score += 2 # Près de PDL pour TP
        if bias == "BULLISH" and price <= (pdh * 0.999): score += 2 # Près de PDH pour TP

        quality = "💎 A+ SETUP" if score >= 10 else "✅ A SETUP" if score >= 7 else "⚖️ B SETUP" if score >= 4 else "IGNORE"

        return {
            "Actif": ticker, 
            "Signal": bias, 
            "Zone": zone, 
            "Qualité": quality, 
            "Score": score, 
            "HMA 20": hma_t,
            "Midnight": round(m_open, 5),
            "PDH": round(pdh, 5),
            "PDL": round(pdl, 5)
        }

    except Exception as e:
        print(f"Erreur critique analyse {ticker}: {e}")
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
    except Exception as e:
        print(f"Erreur fetch {instrument}: {e}")
        return pd.DataFrame()

# ===============================
# INTERFACE
# ===============================

def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner ICT Audité")

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
            time.sleep(0.2) # Légère pause pour API
            progress.progress((i + 1) / len(assets))

        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)
            
            def color_cells(val):
                if 'BULLISH' in str(val) or '🟢' in str(val): return 'color: #00ff00'
                elif 'BEARISH' in str(val) or '🔴' in str(val): return 'color: #ff4b4b'
                elif 'NEUTRAL' in str(val): return 'color: gray'
                return 'color: white'

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
