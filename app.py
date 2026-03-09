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
# ANALYSE & ICT LOGIC (FINAL FIX)
# ===============================

def get_market_bias(df_daily):
    """
    Logique de Tendance corrigée pour détecter les mouvements baissiers 
    même si la SMA 200 est loin (Tendance intermédiaire).
    """
    if len(df_daily) < 60: return "NEUTRAL"
    
    close = df_daily['close']
    
    # Moyennes
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    # sma200 = close.rolling(200).mean() # On retire la dépendance stricte SMA200 pour le signal court terme
    
    c = close.iloc[-1]
    e21 = ema21.iloc[-1]
    e50 = ema50.iloc[-1]
    
    # Logique Bearish stricte (Prix sous EMA21 et EMA21 sous EMA50)
    bearish_structure = c < e21 and e21 < e50
    
    # Logique Bullish stricte (Prix sur EMA21 et EMA21 sur EMA50)
    bullish_structure = c > e21 and e21 > e50
    
    if bearish_structure:
        return "BEARISH"
    elif bullish_structure:
        return "BULLISH"
    else:
        return "NEUTRAL"

def analyze_asset(client, ticker):
    try:
        # 1. Récupération Données
        df_d = fetch_oanda_data(client, ticker, "D", 250)
        df_m15 = fetch_oanda_data(client, ticker, "M15", 200)
        
        if df_d.empty or df_m15.empty: return None

        price = df_m15['close'].iloc[-1]
        
        # 2. BIAIS (Tendance Journalière)
        bias = get_market_bias(df_d)
        
        # 3. MIDNIGHT OPEN (00:00 NEW YORK)
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        
        now_ny = datetime.now(ny_tz)
        today_ny_date = now_ny.date()
        
        # Recherche stricte de la bougie 00:00
        midnight_open = np.nan
        
        # On cherche dans les données M15 la bougie de 00:00 d'aujourd'hui
        today_data = df_m15[df_m15.index.date == today_ny_date]
        
        if not today_data.empty:
            # Prendre l'open de la première bougie disponible du jour (souvent 00:00 ou 17:00 la veille selon le broker)
            # Pour être précis "Midnight NY", on filtre l'heure
            midnight_candle = today_data[(today_data.index.hour == 0) & (today_data.index.minute == 0)]
            if not midnight_candle.empty:
                midnight_open = midnight_candle['open'].iloc[0]
            else:
                # Si pas de bougie 00:00 (market closed), on prend l'open du jour précédent à 17h ou le début des données
                # Fallback simple : Open de la journée en cours
                midnight_open = today_data['open'].iloc[0]
        else:
            # Si on est dimanche soir / lundi matin, on prend l'open de la semaine
            midnight_open = df_m15['open'].iloc[0]

        # 4. PREVIOUS DAY HIGH/LOW (PDH/PDL)
        # Prendre les valeurs de la bougie journalière complétée (iloc[-2])
        if len(df_d) >= 2:
            pdh = df_d['high'].iloc[-2]
            pdl = df_d['low'].iloc[-2]
        else:
            pdh, pdl = np.nan, np.nan

        # 5. ZONES PREMIUM / DISCOUNT
        # LOGIQUE STRICTE :
        # Prix > Midnight Open = PREMIUM
        # Prix < Midnight Open = DISCOUNT
        
        if price > midnight_open:
            zone = "PREMIUM 🔴" 
        else:
            zone = "DISCOUNT 🟢"
            
        # 6. SCORE V10
        score = 0
        
        # A. Tendance
        if bias == "BULLISH": score += 3
        elif bias == "BEARISH": score += 3 # Le score est neutre sur la direction, mais le signal compte
        
        # B. HMA 20 (Momentum Court Terme)
        hma = QuantEngine.hma(df_m15['close'], 20)
        hma_t = "BULLISH" if hma.iloc[-1] > hma.iloc[-2] else "BEARISH"
        
        # Alignement Momentum/Tendance
        if bias == hma_t: score += 4
        
        # C. FVG (Fair Value Gap) sur M15
        last_fvg_bull = df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]
        last_fvg_bear = df_m15['high'].iloc[-1] < df_m15['low'].iloc[-3]
        
        if bias == "BULLISH" and last_fvg_bull: score += 3
        if bias == "BEARISH" and last_fvg_bear: score += 3

        # D. Position Ideal (Bonus Proximité liquidités)
        # Si Bearish et qu'on est proche du PDH (Resistance) -> Bonus
        # Si Bullish et qu'on est proche du PDL (Support) -> Bonus
        try:
            if bias == "BEARISH" and not np.isnan(pdh) and price >= (pdh * 0.998): score += 2
            if bias == "BULLISH" and not np.isnan(pdl) and price <= (pdl * 1.002): score += 2
        except:
            pass

        # Qualité
        quality = "💎 A+ SETUP" if score >= 10 else "✅ A SETUP" if score >= 7 else "⚖️ B SETUP" if score >= 4 else "IGNORE"

        return {
            "Actif": ticker, 
            "Signal": bias, 
            "Zone": zone, 
            "Qualité": quality, 
            "Score": score, 
            "HMA 20": hma_t,
            "Midnight": round(midnight_open, 5),
            "Prix": round(price, 5),
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
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner ICT Final")

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
            time.sleep(0.2) 
            progress.progress((i + 1) / len(assets))

        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)
            
            def color_cells(val):
                if 'BULLISH' in str(val) or '🟢' in str(val): return 'color: #00ff00'
                elif 'BEARISH' in str(val) or '🔴' in str(val): return 'color: #ff4b4b'
                elif 'NEUTRAL' in str(val): return 'color: gray'
                return 'color: white'

            # Affichage avec les colonnes Prix vs Midnight pour vérification visuelle
            cols_to_show = ["Actif", "Signal", "Zone", "Qualité", "Score", "Prix", "Midnight", "PDH", "PDL", "HMA 20"]
            
            st.dataframe(
                df[cols_to_show].style.applymap(color_cells, subset=['Signal', 'HMA 20', 'Zone'])
                .format({"Midnight": "{:.5f}", "Prix": "{:.5f}", "PDH": "{:.5f}", "PDL": "{:.5f}"}), 
                use_container_width=True
            )
            st.success("Scan terminé !")
        else:
            st.warning("Aucun résultat.")

if __name__ == "__main__":
    main()
