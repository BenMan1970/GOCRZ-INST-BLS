import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timezone
import pytz
import time

# ===============================
# ENGINE TECHNIQUE (BLUESTAR V10)
# ===============================
class QuantEngine:
    @staticmethod
    def wma(series, period):
        """Moyenne Mobile Pondérée pour le calcul de la HMA"""
        weights = np.arange(1, period + 1)
        return series.rolling(period).apply(lambda prices: np.dot(prices, weights) / weights.sum(), raw=True)

    @staticmethod
    def hma_smoothed(series, period=20):
        """HMA 20 + EMA 5 Smoothing (Logique Bluestar GPS)"""
        half = int(period / 2)
        sqrt = int(np.sqrt(period))
        wma1 = QuantEngine.wma(series, half)
        wma2 = QuantEngine.wma(series, period)
        raw_hma = 2 * wma1 - wma2
        hma = QuantEngine.wma(raw_hma, sqrt)
        # Lissage final EMA 5 comme dans le code TradingView
        return hma.ewm(span=5, adjust=False).mean()

    @staticmethod
    def calculate_adx(df, period=14):
        """Calcul de l'ADX pour mesurer la force de la tendance"""
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        up, down = high.diff(), -low.diff()
        pdm = pd.Series(np.where((up > down) & (up > 0), up, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        mdm = pd.Series(np.where((down > up) & (down > 0), down, 0), index=df.index).ewm(alpha=1/period, adjust=False).mean()
        adx = (abs((pdm/atr) - (mdm/atr)) / ((pdm/atr) + (mdm/atr)) * 100).ewm(alpha=1/period, adjust=False).mean()
        return adx.iloc[-1]

# ===============================
# LOGIQUE D'ANALYSE PAR ACTIF
# ===============================
def analyze_asset(client, ticker):
    try:
        # Récupération des données (D1 pour le biais, H1 pour ADX, M15 pour l'exécution)
        df_d = fetch_oanda_data(client, ticker, "D", 10)
        df_h1 = fetch_oanda_data(client, ticker, "H1", 50)
        df_m15 = fetch_oanda_data(client, ticker, "M15", 100)

        if df_d.empty or df_m15.empty or len(df_m15) < 30:
            return None

        price = df_m15['close'].iloc[-1]
        
        # 1. MIDNIGHT OPEN (Heure de New York)
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        today_ny = datetime.now(ny_tz).date()
        m15_today = df_m15[df_m15.index.date == today_ny]
        # Si la session NY n'a pas encore ouvert, on prend la première bougie dispo
        midnight_open = m15_today['open'].iloc[0] if not m15_today.empty else df_m15['open'].iloc[0]

        # 2. BIAIS DAILY (Prix vs Moyenne Daily)
        ema_d = df_d['close'].rolling(5).mean().iloc[-1]
        bias = "BULLISH" if price > ema_d else "BEARISH"

        # 3. PREMIUM / DISCOUNT (Correctif : Basé sur Midnight Open)
        zone = "NEUTRAL"
        if bias == "BULLISH" and price < midnight_open:
            zone = "DISCOUNT (BUY) 🟢"
        elif bias == "BEARISH" and price > midnight_open:
            zone = "PREMIUM (SELL) 🔴"

        # 4. HMA 20 MOMENTUM
        hma = QuantEngine.hma_smoothed(df_m15['close'], 20)
        hma_trend = "BULLISH" if hma.iloc[-1] > hma.iloc[-2] else "BEARISH"

        # 5. CALCUL DU SCORE (Sur 14 points)
        score = 0
        if bias == "BULLISH": score += 3
        if (bias == "BULLISH" and price > df_h1['close'].iloc[-1]): score += 2 # Alignement H1
        if hma_trend == bias: score += 1
        if QuantEngine.calculate_adx(df_h1) > 22: score += 1
        
        # Détection simple de FVG (Fair Value Gap)
        if (bias == "BULLISH" and df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]):
            score += 3
        
        # Rebond M15 (Bougie actuelle vs Ouverture)
        if (bias == "BULLISH" and price > df_m15['open'].iloc[-1]):
            score += 2

        # Classification
        quality = "IGNORE"
        if score >= 12: quality = "💎 A+ SETUP"
        elif score >= 9: quality = "✅ A SETUP"
        elif score >= 7: quality = "⚖️ B SETUP"

        # Calcul de la fraîcheur du signal
        diff = datetime.now(timezone.utc) - df_m15.index[-1].to_pydatetime().astimezone(timezone.utc)
        freshness = f"{int(diff.total_seconds() // 3600)}h {int((diff.total_seconds() % 3600) // 60)}m"

        return {
            "Actif": ticker, "Signal": bias, "Zone": zone, "Qualité": quality,
            "Score": score, "HMA 20": hma_trend, "Fraîcheur": freshness
        }
    except Exception:
        return None

def fetch_oanda_data(client, instrument, granularity, count):
    """Récupère les bougies via l'API Oanda v20"""
    try:
        r = instruments.InstrumentsCandles(instrument=instrument, params={"count": count, "granularity": granularity})
        client.request(r)
        data = []
        for c in r.response.get("candles", []):
            if c["complete"]:
                data.append({
                    "time": pd.to_datetime(c["time"]),
                    "open": float(c["mid"]["o"]),
                    "high": float(c["mid"]["h"]),
                    "low": float(c["mid"]["l"]),
                    "close": float(c["mid"]["c"])
                })
        df = pd.DataFrame(data)
        if not df.empty:
            df.set_index("time", inplace=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize('UTC')
        return df
    except:
        return pd.DataFrame()

# ===============================
# INTERFACE STREAMLIT
# ===============================
def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner Manuel")

    # Utilisation de la clé API configurée dans les secrets
    if "OANDA_TOKEN" not in st.secrets:
        st.error("ERREUR : La clé 'OANDA_TOKEN' est introuvable dans les secrets Streamlit.")
        st.stop()

    client = oandapyV20.API(access_token=st.secrets["OANDA_TOKEN"], environment="practice")

    # Liste des 33 actifs à scanner
    assets = [
        "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "NZD_USD", "USD_CHF",
        "EUR_JPY", "GBP_JPY", "EUR_GBP", "XAU_USD", "US30_USD", "NAS100_USD", "DE30_EUR",
        "AUD_JPY", "EUR_AUD", "GBP_AUD", "CAD_JPY", "EUR_CAD", "GBP_CAD", "CHF_JPY"
    ]

    if st.button("LANCER LE SCANNER 🚀", use_container_width=True):
        results = []
        progress_bar = st.progress(0)
        
        for i, ticker in enumerate(assets):
            res = analyze_asset(client, ticker)
            if res:
                results.append(res)
            # Petite pause pour éviter de saturer l'API et bloquer l'app
            time.sleep(0.15) 
            progress_bar.progress((i + 1) / len(assets))

        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)
            
            # Application des couleurs sur le tableau final
            def style_signal(val):
                color = '#00ff00' if "BULLISH" in str(val) else '#ff4b4b'
                return f'color: {color}; font-weight: bold'

            st.dataframe(
                df.style.applymap(style_signal, subset=['Signal', 'HMA 20'])
                .background_gradient(cmap='RdYlGn', subset=['Score'], vmin=0, vmax=14),
                use_container_width=True, height=600
            )
        else:
            st.warning("Aucun résultat. Vérifiez les symboles ou la clé API.")

if __name__ == "__main__":
    main()
