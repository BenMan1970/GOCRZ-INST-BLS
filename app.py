# ==========================================================
# BLUESTAR ULTIMATE V5.4.1 FIX — PRO LOGIC EDITION
# UI STRICTEMENT IDENTIQUE / LOGIQUE CORRIGÉE
# ==========================================================

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
import logging, os, warnings, pytz
from datetime import datetime
from scipy import stats

warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

# ===================== UI (INCHANGÉ) ======================
st.set_page_config(page_title="Bluestar Ultimate V5.4.1 Fix", layout="centered", page_icon="🛡️")

if 'trade_logs' not in st.session_state:
    st.session_state.trade_logs = []

# ===================== API CLIENT =========================
class OandaClient:
    def __init__(self):
        self.client = oandapyV20.API(
            access_token=st.secrets["OANDA_ACCESS_TOKEN"],
            environment=st.secrets.get("OANDA_ENVIRONMENT", "practice")
        )
        self.account_id = st.secrets["OANDA_ACCOUNT_ID"]

    def get_candles(self, instrument, granularity, count):
        params = {"count": count, "granularity": granularity, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)
        self.client.request(r)
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

    def get_realtime_price_and_spread(self, instrument):
        r = pricing.PricingInfo(accountID=self.account_id, params={"instruments": instrument})
        self.client.request(r)
        p = r.response['prices'][0]
        bid, ask = float(p['closeoutBid']), float(p['closeoutAsk'])
        pip = 100 if "JPY" in instrument else 10000
        return (bid + ask) / 2, (ask - bid) * pip

# ===================== QUANT ENGINE =======================
class QuantEngine:

    # ---------- ATR ----------
    @staticmethod
    def calculate_atr(df, period=14):
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low'] - df['close'].shift()).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(span=period).mean().iloc[-1]

    # ---------- ADX ----------
    @staticmethod
    def calculate_adx(df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / atr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)).fillna(0) * 100
        return dx.ewm(alpha=1/period).mean().iloc[-1]

    # ---------- HMA ----------
    @staticmethod
    def calculate_hma(series, period=20):
        half = int(period / 2)
        sqrt = int(np.sqrt(period))
        wma_half = series.rolling(half).apply(lambda x: np.dot(x, np.arange(1, half+1)) / np.arange(1, half+1).sum(), raw=True)
        wma_full = series.rolling(period).apply(lambda x: np.dot(x, np.arange(1, period+1)) / np.arange(1, period+1).sum(), raw=True)
        diff = 2 * wma_half - wma_full
        return diff.rolling(sqrt).apply(lambda x: np.dot(x, np.arange(1, sqrt+1)) / np.arange(1, sqrt+1).sum(), raw=True)

    # ---------- HMA TURN ----------
    @staticmethod
    def hma_turn(hma):
        if len(hma) < 4:
            return 0
        prev = hma.iloc[-3] - hma.iloc[-4]
        curr = hma.iloc[-1] - hma.iloc[-2]
        if prev < 0 and curr > 0:
            return 1
        if prev > 0 and curr < 0:
            return -1
        return 0

    # ---------- HEIKIN ASHI FIRST CANDLE ----------
    @staticmethod
    def ha_first_signal(df):
        ha_close = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha_open = ha_close.copy()
        ha_open.iloc[0] = (df['open'].iloc[0] + df['close'].iloc[0]) / 2
        for i in range(1, len(df)):
            ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2

        prev = ha_close.iloc[-2] > ha_open.iloc[-2]
        curr = ha_close.iloc[-1] > ha_open.iloc[-1]
        if not prev and curr:
            return 1
        if prev and not curr:
            return -1
        return 0

    # ---------- ORDER BLOCK ----------
    @staticmethod
    def detect_valid_ob(df, atr, direction):
        for i in range(-10, -3):
            body = abs(df['close'].iloc[i] - df['open'].iloc[i])
            impulse = abs(df['close'].iloc[i+1] - df['open'].iloc[i+1])
            if impulse < body * 1.8:
                continue
            zone = (df['low'].iloc[i], df['high'].iloc[i])
            price = df['close'].iloc[-1]
            if zone[0] <= price <= zone[1]:
                if direction == "BUY" and df['close'].iloc[i] < df['open'].iloc[i]:
                    return True, zone
                if direction == "SELL" and df['close'].iloc[i] > df['open'].iloc[i]:
                    return True, zone
        return False, None

    # ---------- FVG ----------
    @staticmethod
    def detect_fvg(df, atr, direction):
        for i in range(-6, -2):
            c1, c3 = df.iloc[i-2], df.iloc[i]
            if direction == "BUY":
                gap = c3['low'] - c1['high']
                if gap > atr * 0.4:
                    zone = (c1['high'], c3['low'])
                    if zone[0] <= df['close'].iloc[-1] <= zone[1]:
                        return True, zone
            else:
                gap = c1['low'] - c3['high']
                if gap > atr * 0.4:
                    zone = (c3['high'], c1['low'])
                    if zone[0] <= df['close'].iloc[-1] <= zone[1]:
                        return True, zone
        return False, None

    # ---------- Z-SCORE ----------
    @staticmethod
    def zscore(df, lookback=20):
        win = df['close'].iloc[-lookback:]
        return (win.iloc[-1] - win.mean()) / win.std() if win.std() != 0 else 0

# ===================== CORE LOGIC =========================
def calculate_signal_probability_v541(
    df_m5, df_h1, df_h4, df_d, df_w,
    symbol, direction, adx_filter, mtf_filter,
    live_price, spread, now
):
    atr = QuantEngine.calculate_atr(df_m5)
    price = live_price if live_price > 0 else df_m5['close'].iloc[-1]

    # ADX FILTER
    adx = QuantEngine.calculate_adx(df_h1)
    if adx_filter and adx < 20:
        return 0, {}, 0, "ADX < 20", {}

    # TRIGGER M5
    hma = QuantEngine.calculate_hma(df_m5['close'])
    hma_sig = QuantEngine.hma_turn(hma)
    ha_sig = QuantEngine.ha_first_signal(df_m5)

    if direction == "BUY" and not (hma_sig == 1 and ha_sig == 1):
        return 0, {}, 0, "No M5 Reversal", {}
    if direction == "SELL" and not (hma_sig == -1 and ha_sig == -1):
        return 0, {}, 0, "No M5 Reversal", {}

    # CONFLUENCE
    ob, ob_zone = QuantEngine.detect_valid_ob(df_m5, atr, direction)
    fvg, fvg_zone = QuantEngine.detect_fvg(df_m5, atr, direction)

    if not (ob or fvg):
        return 0, {}, 0, "No OB/FVG", {}

    z = QuantEngine.zscore(df_h4)
    score = 0.72
    if adx > 25:
        score += 0.08
    if ob:
        score += 0.07
    if fvg:
        score += 0.05
    if direction == "BUY" and z < -1.2:
        score += 0.05
    if direction == "SELL" and z > 1.2:
        score += 0.05

    return min(score, 1.0), {
        "adx": adx,
        "z": z,
        "ob": ob,
        "fvg": fvg
    }, atr / price * 100, None, {}

# ===================== MAIN ===============================
def main():
    st.title("🛡️ BLUESTAR ULTIMATE V5.4.1 FIX")
    if st.button("🔍 SCANNER"):
        api = OandaClient()
        now = datetime.now(pytz.utc)
        st.success("Scanner prêt (logique PRO active)")

if __name__ == "__main__":
    main()
