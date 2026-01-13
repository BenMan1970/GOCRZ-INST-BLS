# BlueStar Scanner v8.1 – Intraday Pro (Institutional Bonus)
# SAME UI / SAME INDICATORS / SAME DESIGN
# ADDITION: OB / FVG / VOLUME PROFILE AS NON-BLOCKING SCORING

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from concurrent.futures import ThreadPoolExecutor
import warnings
warnings.filterwarnings('ignore')

st.set_page_config(page_title="BlueStar Sniper Intraday Pro v8.1", layout="centered", page_icon="⭐")

# =============================
# DATA
# =============================
@st.cache_data(ttl=300)
def fetch_candles(api, instrument, granularity, count):
    params = {"count": count, "granularity": granularity, "price": "M"}
    r = instruments.InstrumentsCandles(instrument=instrument, params=params)
    api.request(r)
    data = []
    for c in r.response['candles']:
        if c['complete']:
            data.append({
                'time': pd.to_datetime(c['time']),
                'open': float(c['mid']['o']),
                'high': float(c['mid']['h']),
                'low': float(c['mid']['l']),
                'close': float(c['mid']['c']),
                'volume': int(c['volume'])
            })
    return pd.DataFrame(data)

# =============================
# INDICATORS (UNCHANGED QUALITY)
# =============================
class I:
    @staticmethod
    def atr(df, p=14):
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low'] - df['close'].shift()).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1/p, adjust=False).mean().iloc[-1]

    @staticmethod
    def rsi(series, p=14):
        d = series.diff()
        g = d.where(d > 0, 0)
        l = -d.where(d < 0, 0)
        rs = g.ewm(alpha=1/p, adjust=False).mean() / l.ewm(alpha=1/p, adjust=False).mean()
        return 100 - (100 / (1 + rs))

    @staticmethod
    def adx(df, p=14):
        h, l, c = df['high'], df['low'], df['close']
        up = h.diff()
        dn = -l.diff()
        plus = np.where((up > dn) & (up > 0), up, 0)
        minus = np.where((dn > up) & (dn > 0), dn, 0)
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/p, adjust=False).mean()
        plus_di = 100 * pd.Series(plus).ewm(alpha=1/p, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus).ewm(alpha=1/p, adjust=False).mean() / atr
        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
        return dx.ewm(alpha=1/p, adjust=False).mean().iloc[-1]

    @staticmethod
    def hma(series, p):
        half = int(p/2)
        sqrt = int(np.sqrt(p))
        wma = lambda s, l: s.rolling(l).apply(lambda x: np.dot(x, np.arange(1,l+1)) / np.arange(1,l+1).sum(), raw=True)
        return wma(2*wma(series, half)-wma(series, p), sqrt)

# =============================
# INSTITUTIONAL ZONES (BONUS)
# =============================
def detect_order_block(df, atr, direction):
    for i in range(-30, -3):
        c = df.iloc[i]
        n = df.iloc[i+1]
        body = abs(c['close'] - c['open'])
        if body < atr * 0.2:
            continue
        if direction == 'BUY' and c['close'] < c['open'] and n['close'] > n['open']:
            return True
        if direction == 'SELL' and c['close'] > c['open'] and n['close'] < n['open']:
            return True
    return False


def detect_fvg(df, atr, direction):
    for i in range(-25, -2):
        c1, c2, c3 = df.iloc[i-2], df.iloc[i-1], df.iloc[i]
        if direction == 'BUY' and c3['low'] - c1['high'] > atr * 0.2:
            return True
        if direction == 'SELL' and c1['low'] - c3['high'] > atr * 0.2:
            return True
    return False


def volume_profile_bias(df):
    mid = (df['high'] + df['low']) / 2
    vwap = (mid * df['volume']).sum() / df['volume'].sum()
    price = df['close'].iloc[-1]
    if price > vwap:
        return 'PREMIUM'
    if price < vwap:
        return 'DISCOUNT'
    return 'NEUTRAL'

# =============================
# STRUCTURE
# =============================
def structure(df, direction):
    if direction == 'BUY':
        return df['high'].iloc[-5:].max() > df['high'].iloc[-20:-5].max()
    return df['low'].iloc[-5:].min() < df['low'].iloc[-20:-5].min()

# =============================
# SCANNER LOGIC V8.1
# =============================
def scan_v8(df_m5, df_m15, df_h1, symbol, direction):
    score = 0
    notes = []

    price = df_m5['close'].iloc[-1]
    atr = I.atr(df_m5)

    # ---- HARD FILTERS ----
    if not structure(df_h1, direction):
        return None
    score += 20; notes.append('H1 Structure')

    if I.adx(df_h1) < 15:
        return None
    score += 15; notes.append('H1 Momentum')

    hma20 = I.hma(df_m5['close'], 20).iloc[-1]
    if direction == 'BUY' and price > hma20 + atr*0.5:
        return None
    if direction == 'SELL' and price < hma20 - atr*0.5:
        return None
    score += 15; notes.append('Pullback Zone')

    # ---- SOFT FILTERS ----
    rsi_h1 = I.rsi(df_h1['close']).iloc[-1]
    if direction == 'BUY' and rsi_h1 > 55:
        score += 10; notes.append('RSI H1 Bullish')
    if direction == 'SELL' and rsi_h1 < 45:
        score += 10; notes.append('RSI H1 Bearish')

    hma_m15 = I.hma(df_m15['close'], 20)
    if direction == 'BUY' and hma_m15.iloc[-1] > hma_m15.iloc[-2]:
        score += 10; notes.append('M15 Trend')
    if direction == 'SELL' and hma_m15.iloc[-1] < hma_m15.iloc[-2]:
        score += 10; notes.append('M15 Trend')

    if I.adx(df_m5) > 18:
        score += 10; notes.append('M5 Momentum')

    rsi_m5 = I.rsi(df_m5['close'], 10).iloc[-1]
    if direction == 'BUY' and rsi_m5 > 50:
        score += 10; notes.append('RSI Break')
    if direction == 'SELL' and rsi_m5 < 50:
        score += 10; notes.append('RSI Break')

    # ---- INSTITUTIONAL BONUS ----
    if detect_order_block(df_m5, atr, direction):
        score += 8; notes.append('Order Block')

    if detect_fvg(df_m5, atr, direction):
        score += 6; notes.append('FVG')

    vp = volume_profile_bias(df_h1)
    if direction == 'BUY' and vp == 'DISCOUNT':
        score += 6; notes.append('VP Discount')
    if direction == 'SELL' and vp == 'PREMIUM':
        score += 6; notes.append('VP Premium')

    quality = 'WATCHLIST'
    if score >= 80: quality = 'ELITE'
    elif score >= 65: quality = 'PREMIUM'

    return {
        'symbol': symbol,
        'direction': direction,
        'score': score,
        'quality': quality,
        'notes': notes,
        'price': price
    }

# =============================
# APP
# =============================
def main():
    st.title('⭐ BlueStar Scanner v8.1 – Intraday Pro')
    st.caption('Institutional Confluence – Human Validation Required')

    if st.button('SCAN'):
        api = oandapyV20.API(access_token=st.secrets['OANDA_ACCESS_TOKEN'])
        assets = ['EUR_USD','GBP_USD','USD_JPY','XAU_USD']
        results = []

        for s in assets:
            df_m5 = fetch_candles(api, s, 'M5', 300)
            df_m15 = fetch_candles(api, s, 'M15', 200)
            df_h1 = fetch_candles(api, s, 'H1', 300)
            if df_m5.empty or df_m15.empty or df_h1.empty:
                continue
            for d in ['BUY','SELL']:
                r = scan_v8(df_m5, df_m15, df_h1, s, d)
                if r:
                    results.append(r)

        if not results:
            st.warning('No opportunities')
        else:
            for r in sorted(results, key=lambda x: x['score'], reverse=True):
                st.success(f"{r['symbol']} {r['direction']} | {r['quality']} | {r['score']}")
                st.caption(' · '.join(r['notes']))

if __name__ == '__main__':
    main()
