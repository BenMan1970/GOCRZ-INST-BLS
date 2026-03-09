import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timezone
import pytz

# ===============================
# ENGINE TECHNIQUE (TV INSPIRED)
# ===============================
class BluestarEngine:
    @staticmethod
    def wma(series, period):
        weights = np.arange(1, period + 1)
        return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

    @staticmethod
    def hma_smoothed(series, period=20):
        # Logique HMA du code TV : ta.hma(20) + ta.ema(5)
        half = int(period / 2)
        sqrt = int(np.sqrt(period))
        wma1 = BluestarEngine.wma(series, half)
        wma2 = BluestarEngine.wma(series, period)
        raw_hma = 2 * wma1 - wma2
        hma = BluestarEngine.wma(raw_hma, sqrt)
        return hma.ewm(span=5, adjust=False).mean() # EMA 5 Smoothing

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
# ANALYSE INSTITUTIONNELLE
# ===============================
def analyze_asset(client, ticker):
    # Fetch data
    df_d = fetch_data(client, ticker, "D", 5)
    df_h4 = fetch_data(client, ticker, "H4", 50)
    df_h1 = fetch_data(client, ticker, "H1", 50)
    df_m15 = fetch_data(client, ticker, "M15", 100)

    if df_d.empty or df_m15.empty: return None

    price = df_m15['close'].iloc[-1]
    pdh, pdl = df_d['high'].iloc[-2], df_d['low'].iloc[-2]
    
    # 1. MIDNIGHT OPEN (NY TIME)
    ny_tz = pytz.timezone('America/New_York')
    df_m15.index = df_m15.index.tz_convert(ny_tz)
    try:
        midnight_open = df_m15[df_m15.index.hour == 0].iloc[0]['open']
    except:
        midnight_open = df_m15['open'].iloc[0]

    # 2. DETERMINATION DU BIAIS (Daily + MTF)
    # Simple score MTF: 1 point par TF si prix > EMA 50
    score_mtf = 0
    if price > df_d['close'].rolling(50).mean().iloc[-1]: score_mtf += 1
    if price > df_h4['close'].rolling(50).mean().iloc[-1]: score_mtf += 1
    if price > df_h1['close'].rolling(50).mean().iloc[-1]: score_mtf += 1
    
    bias = "BULLISH" if score_mtf >= 2 else "BEARISH"
    
    # 3. PREMIUM / DISCOUNT (Correction demandée)
    # Discount = Sous Midnight Open ET Proche PDL
    zone = "NEUTRAL"
    if price < midnight_open:
        zone = "DISCOUNT (BUY)" if price < (pdl + (pdh-pdl)*0.5) else "NEUTRAL"
    elif price > midnight_open:
        zone = "PREMIUM (SELL)" if price > (pdl + (pdh-pdl)*0.5) else "NEUTRAL"

    # 4. HMA 20 COLORED
    hma = BluestarEngine.hma_smoothed(df_m15['close'], 20)
    hma_trend = "BULLISH" if hma.iloc[-1] > hma.iloc[-2] else "BEARISH"

    # 5. SCORE DE QUALITÉ (Barème 14 points)
    final_score = 0
    if bias == "BULLISH": final_score += 3
    if score_mtf == 3: final_score += 2
    if hma_trend == bias: final_score += 1
    # ADX Filter
    adx_v = BluestarEngine.get_adx(df_h1)
    if adx_v > 22: final_score += 1
    # FVG Check (Simple)
    if df_m15['low'].iloc[-1] > df_m15['high'].iloc[-3]: final_score += 3 # Bullish FVG
    
    quality = "IGNORE"
    if final_score >= 12: quality = "💎 A+ SETUP"
    elif final_score >= 9: quality = "✅ A SETUP"
    elif final_score >= 7: quality = "⚖️ B SETUP"

    # Fraîcheur
    diff = datetime.now(timezone.utc) - df_m15.index[-1].to_pydatetime().astimezone(timezone.utc)
    freshness = f"{int(diff.total_seconds() // 3600)}h {int((diff.total_seconds() % 3600) // 60)}m"

    return {
        "Actif": ticker, "Signal": bias, "Zone": zone, "Qualité": quality,
        "Score": final_score, "HMA 20": hma_trend, "Fraîcheur": freshness
    }

# (Fonction fetch_data et main() Streamlit identiques au standard OANDA)
