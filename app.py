import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from concurrent.futures import ThreadPoolExecutor
import warnings

warnings.filterwarnings("ignore")

# -----------------------------
# CONFIG
# -----------------------------

st.set_page_config(page_title="GO-CRZ Institutional Scanner", layout="wide")

OANDA_TOKEN = st.secrets["OANDA_ACCESS_TOKEN"]
ACCOUNT_ID = st.secrets["OANDA_ACCOUNT_ID"]
ENV = st.secrets.get("OANDA_ENVIRONMENT","practice")

client = oandapyV20.API(access_token=OANDA_TOKEN, environment=ENV)

# Instruments à scanner
INSTRUMENTS = [
"EUR_USD","GBP_USD","USD_JPY","AUD_USD","USD_CAD",
"NZD_USD","EUR_JPY","GBP_JPY","EUR_GBP","XAU_USD"
]

# -----------------------------
# DATA DOWNLOAD
# -----------------------------

@st.cache_data(ttl=300)
def get_candles(symbol, tf, count=500):

    params = {
        "granularity": tf,
        "count": count,
        "price": "M"
    }

    r = instruments.InstrumentsCandles(
        instrument=symbol,
        params=params
    )

    client.request(r)

    data=[]

    for c in r.response["candles"]:
        if c["complete"]:
            data.append({
                "time": c["time"],
                "open": float(c["mid"]["o"]),
                "high": float(c["mid"]["h"]),
                "low": float(c["mid"]["l"]),
                "close": float(c["mid"]["c"])
            })

    return pd.DataFrame(data)

# -----------------------------
# INDICATORS
# -----------------------------

def EMA(series, period):
    return series.ewm(span=period).mean()

def ATR(df, period=14):

    high_low = df.high - df.low
    high_close = abs(df.high - df.close.shift())
    low_close = abs(df.low - df.close.shift())

    tr = pd.concat([high_low,high_close,low_close],axis=1).max(axis=1)

    return tr.rolling(period).mean()

def ADX(df, period=14):

    up = df.high.diff()
    down = df.low.diff()

    plus_dm = np.where((up>down) & (up>0),up,0)
    minus_dm = np.where((down>up) & (down>0),down,0)

    tr = ATR(df)

    plus_di = 100*(pd.Series(plus_dm).rolling(period).mean()/tr)
    minus_di = 100*(pd.Series(minus_dm).rolling(period).mean()/tr)

    dx = abs(plus_di-minus_di)/(plus_di+minus_di)*100

    return dx.rolling(period).mean()

# -----------------------------
# FAIR VALUE GAP
# -----------------------------

def detect_fvg(df):

    bullish=[]
    bearish=[]

    for i in range(2,len(df)):

        if df.low.iloc[i] > df.high.iloc[i-2]:
            bullish.append((df.high.iloc[i-2],df.low.iloc[i]))

        if df.high.iloc[i] < df.low.iloc[i-2]:
            bearish.append((df.high.iloc[i],df.low.iloc[i-2]))

    return bullish,bearish

# -----------------------------
# PREMIUM / DISCOUNT
# -----------------------------

def premium_discount(df):

    prev_high=df.high.iloc[-2]
    prev_low=df.low.iloc[-2]

    midpoint=(prev_high+prev_low)/2
    price=df.close.iloc[-1]

    if price < midpoint:
        return "DISCOUNT"
    else:
        return "PREMIUM"

# -----------------------------
# TREND BIAS
# -----------------------------

def trend_bias(df):

    ema21=EMA(df.close,21)
    ema50=EMA(df.close,50)
    ema200=EMA(df.close,200)

    close=df.close.iloc[-1]

    if close > ema200.iloc[-1] and ema21.iloc[-1] > ema50.iloc[-1]:
        return "BULLISH"

    if close < ema200.iloc[-1] and ema21.iloc[-1] < ema50.iloc[-1]:
        return "BEARISH"

    return "NEUTRAL"

# -----------------------------
# ATR EXPANSION
# -----------------------------

def atr_expansion(df):

    atr=ATR(df)

    atr_mean=atr.rolling(20).mean()

    return atr.iloc[-1] > atr_mean.iloc[-1]

# -----------------------------
# ADR REMAINING RANGE
# -----------------------------

def adr_remaining(df):

    daily_range = df.high - df.low
    adr = daily_range.rolling(20).mean().iloc[-1]

    today_range = daily_range.iloc[-1]

    remaining = 1 - (today_range/adr)

    return round(remaining*100,1)

# -----------------------------
# VOLATILITY SCORE
# -----------------------------

def volatility_filter(df):

    score=0

    adx=ADX(df).iloc[-1]

    if adx>20:
        score+=1

    if atr_expansion(df):
        score+=1

    return score, round(adx,1)

# -----------------------------
# MAIN ANALYSIS
# -----------------------------

def analyze(symbol):

    d1=get_candles(symbol,"D")
    h4=get_candles(symbol,"H4")
    h1=get_candles(symbol,"H1")

    if len(d1)<200:
        return None

    bias=trend_bias(d1)
    zone=premium_discount(d1)

    bull_fvg,bear_fvg=detect_fvg(h4)

    fvg=False
    if bull_fvg or bear_fvg:
        fvg=True

    vol_score,adx=volatility_filter(h1)

    adr=adr_remaining(d1)

    # SCORE

    score=0

    if bias!="NEUTRAL":
        score+=3

    if fvg:
        score+=3

    if zone=="DISCOUNT" and bias=="BULLISH":
        score+=2

    if zone=="PREMIUM" and bias=="BEARISH":
        score+=2

    if adx>20:
        score+=1

    score+=vol_score

    return {

        "Symbol":symbol,
        "Bias":bias,
        "Location":zone,
        "ADX":adx,
        "ADR_Remaining_%":adr,
        "Score":score
    }

# -----------------------------
# SCAN
# -----------------------------

def run_scan():

    results=[]

    with ThreadPoolExecutor(max_workers=6) as exe:

        tasks=[exe.submit(analyze,s) for s in INSTRUMENTS]

        for t in tasks:

            r=t.result()

            if r:
                results.append(r)

    df=pd.DataFrame(results)

    return df.sort_values("Score",ascending=False)

# -----------------------------
# UI
# -----------------------------

st.title("⭐ GO-CRZ Institutional Market Scanner")

st.write("Scanner objectif basé sur volatilité, structure et location.")

if st.button("Run Scan"):

    df=run_scan()

    st.dataframe(df,use_container_width=True)
