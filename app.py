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
class BluestarEngine:
    @staticmethod
    def wma(series, period):
        weights = np.arange(1, period + 1)
        return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

    @staticmethod
    def hma_smoothed(series, period=20):
        # HMA 20 + EMA 5 Smoothing (Logique exacte du script TV)
        half = int(period / 2)
        sqrt = int(np.sqrt(period))
        wma1 = BluestarEngine.wma(series, half)
        wma2 = BluestarEngine.wma(series, period)
        raw_hma = 2 * wma1 - wma2
        hma = BluestarEngine.wma(raw_hma, sqrt)
        return hma.ewm(span=5, adjust=False).mean()

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

    @staticmethod
    def get_obv_status(df):
        # Logique Volume Pump/Dump (OBV + Bollinger sur OBV)
        obv = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
        ma_obv = obv.rolling(20).mean()
        std_obv = obv.rolling(20).std()
        upper = ma_obv + 1.5 * std_obv
        lower = ma_obv - 1.5 * std_obv
        
        if obv.iloc[-1] > upper.iloc[-1]: return "PUMP ⚡"
        if obv.iloc[-1] < lower.iloc[-1]: return "DUMP ⚡"
        return "STABLE"

# ===============================
# LOGIQUE D'ANALYSE (SANS ERREURS)
# ===============================
def analyze_asset(client, ticker):
    try:
        df_d = fetch_oanda(client, ticker, "D", 5)
        df_h4 = fetch_oanda(client, ticker, "H4", 50)
        df_h1 = fetch_oanda(client, ticker, "H1", 50)
        df_m15 = fetch_oanda(client, ticker, "M15", 100)

        if df_d.empty or df_m15.empty: return None

        price = df_m15['close'].iloc[-1]
        pdh, pdl = df_d['high'].iloc[-2], df_d['low'].iloc[-2]
        
        # 1. Midnight Open (NY Time)
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_localize('UTC').tz_convert(ny_tz)
        today_ny = datetime.now(ny_tz).date()
        m15_today = df_m15[df_m15.index.date == today_ny]
        midnight_open = m15_today['open'].iloc[0] if not m15_today.empty else df_m15['open'].iloc[0]

        # 2. Biais Institutionnel
        ema50_d = df_d['close'].rolling(50).mean().iloc[-1]
        bias = "BULLISH" if price > ema50_d else "BEARISH"

        # 3. Premium / Discount (Correction stricte)
        # BULLISH: On veut acheter SOUS le Midnight Open (Discount)
        # BEARISH: On veut vendre AU-DESSUS du Midnight Open (Premium)
        zone = "NEUTRAL"
        if bias == "BULLISH" and price < midnight_open: zone = "DISCOUNT (BUY) 🟢"
        elif bias == "BEARISH" and price > midnight_open: zone = "PREMIUM (SELL) 🔴"

        # 4. HMA 20 & Volume
        hma = BluestarEngine.hma_smoothed(df_m15['close'], 20)
        hma_trend = "BULLISH" if hma.iloc[-1] > hma.iloc[-2] else "BEARISH"
        vol_status = BluestarEngine.get_obv_status(df_m15)

        # 5. Score sur 14 points
        score = 0
        if bias == "BULLISH": score += 3
        if price > df_h4['close'].rolling(50).mean().iloc[-1]: score += 2
        if hma_trend == bias: score += 1
        if BluestarEngine.get_adx(df_h1) > 22: score += 1
        # FVG M15 Simple
        if (bias == "BULLISH" and df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]): score += 3
        if (bias == "BULLISH" and price > df_m15['open'].iloc[-1]): score += 2 # Rebond

        quality = "IGNORE"
        if score >= 12: quality = "💎 A+ SETUP"
        elif score >= 9: quality = "✅ A SETUP"
        elif score >= 7: quality = "⚖️ B SETUP"

        # Fraîcheur
        diff = datetime.now(timezone.utc) - df_m15.index[-1].to_pydatetime().astimezone(timezone.utc)
        freshness = f"{int(diff.total_seconds() // 3600)}h {int((diff.total_seconds() % 3600) // 60)}m"

        return {
            "Actif": ticker, "Signal": bias, "Zone": zone, "Qualité": quality,
            "Score": score, "HMA 20": hma_trend, "Volume": vol_status, "Fraîcheur": freshness
        }
    except Exception:
        return None

def fetch_oanda(client, ticker, granularity, count):
    try:
        r = instruments.InstrumentsCandles(instrument=ticker, params={"count": count, "granularity": granularity})
        client.request(r)
        df = pd.DataFrame([{"time": c["time"], "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]), 
                            "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"]), "volume": float(c["volume"])} 
                           for c in r.response.get("candles", []) if c["complete"]])
        if not df.empty:
            df['time'] = pd.to_datetime(df['time'])
            df.set_index('time', inplace=True)
        return df
    except: return pd.DataFrame()

# ===============================
# INTERFACE PRINCIPALE
# ===============================
def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 - Scanner Manuel")

    if "OANDA_ACCESS_TOKEN" not in st.secrets:
        st.error("Clé OANDA_ACCESS_TOKEN manquante dans les Secrets.")
        st.stop()

    client = oandapyV20.API(access_token=st.secrets["OANDA_ACCESS_TOKEN"], environment="practice")

    assets = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "NZD_USD", "USD_CHF", "XAU_USD", "NAS100_USD", "US30_USD", "DE30_EUR"]

    if st.button("LANCER LE SCANNER 🚀", use_container_width=True):
        results = []
        progress = st.progress(0)
        for i, ticker in enumerate(assets):
            res = analyze_asset(client, ticker)
            if res: results.append(res)
            progress.progress((i+1)/len(assets))
        
        if results:
            df = pd.DataFrame(results).sort_values(by="Score", ascending=False)
            
            def style_signal(val):
                color = '#00ff00' if val == "BULLISH" else '#ff4b4b'
                return f'color: {color}; font-weight: bold'

            st.dataframe(
                df.style.applymap(style_signal, subset=['Signal', 'HMA 20'])
                .background_gradient(cmap='RdYlGn', subset=['Score'], vmin=0, vmax=14),
                use_container_width=True, height=600
            )

if __name__ == "__main__":
    main()
