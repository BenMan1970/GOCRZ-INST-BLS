import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
import logging
import time
import csv
import os
from datetime import datetime, timedelta
from scipy import stats 
import pytz
import warnings

# ==========================================
# CONFIGURATION & STYLE (THEME BLEU V5.1 FINAL)
# ==========================================
warnings.simplefilter(action='ignore', category=FutureWarning)
st.set_page_config(page_title="Bluestar Ultimate V5.1", layout="centered", page_icon="🛡️")

LOG_FILE = "bluestar_v51_log.csv"

if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            "timestamp", "symbol", "direction", "price", "score", "hma_m5", "ha_status", 
            "pdh_pdl_status", "fvg_status", "h1_h4_align", "sl", "tp"
        ])

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700;900&display=swap');
    * { font-family: 'Roboto', sans-serif; }
    .stApp { background-color: #0f1117; background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%); }
    .main .block-container { max-width: 950px; padding-top: 2rem; }
    h1 {
        background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        font-weight: 900; font-size: 2.5em; text-align: center; margin-bottom: 0.2em;
    }
    .stButton>button {
        width: 100%; border-radius: 12px; height: 3.5em; font-weight: 700; font-size: 1.1em;
        border: 1px solid rgba(255,255,255,0.1);
        background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%);
        color: white; transition: all 0.2s ease;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5);
    }
    .streamlit-expanderHeader { background-color: #1e293b !important; border: 1px solid #334155; border-radius: 10px; color: #f8fafc !important; padding: 1rem; }
    .streamlit-expanderContent { background-color: #161b22; border: 1px solid #334155; border-top: none; border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; padding: 20px; }
    .badge { color: white; padding: 4px 10px; border-radius: 6px; font-size: 0.75em; font-weight: 700; margin: 2px; display: inline-block; }
    .badge-session { background: linear-gradient(135deg, #db2777 0%, #ec4899 100%); }
    .badge-blue { background: linear-gradient(135deg, #2563eb 0%, #3b82f6 100%); }
    .badge-gold { background: linear-gradient(135deg, #fbbf24 0%, #d97706 100%); color: black; }
    .inst-label { color:#94a3b8; font-size:0.8em; text-transform: uppercase; letter-spacing: 1px; }
    .inst-val { font-size: 1.1em; font-weight: 700; color: #f1f5f9; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# CLIENT API & SESSION STATE
# ==========================================
if 'cache' not in st.session_state: st.session_state.cache = {}
if 'cs_data' not in st.session_state: st.session_state.cs_data = {'data': None, 'time': None}

class OandaClient:
    def __init__(self):
        try:
            self.access_token = st.secrets["OANDA_ACCESS_TOKEN"]
            self.account_id = st.secrets["OANDA_ACCOUNT_ID"]
            self.environment = st.secrets.get("OANDA_ENVIRONMENT", "practice")
            self.client = oandapyV20.API(access_token=self.access_token, environment=self.environment)
        except Exception as e:
            st.error(f"⚠️ Configuration API manquante: {e}")
            st.stop()

    def get_candles(self, instrument, granularity, count):
        key = f"{instrument}_{granularity}_{count}" 
        if key in st.session_state.cache:
            ts, data = st.session_state.cache[key]
            if (datetime.now() - ts).total_seconds() < 60: return data

        try:
            params = {"count": count, "granularity": granularity, "price": "M"}
            r = instruments.InstrumentsCandles(instrument=instrument, params=params)
            self.client.request(r)
            data = []
            for c in r.response['candles']:
                if c['complete']:
                    data.append({
                        'time': pd.to_datetime(c['time']),
                        'open': float(c['mid']['o']), 'high': float(c['mid']['h']),
                        'low': float(c['mid']['l']), 'close': float(c['mid']['c']),
                        'volume': int(c['volume'])
                    })
            df = pd.DataFrame(data)
            if not df.empty:
                st.session_state.cache[key] = (datetime.now(), df)
            return df
        except Exception as e:
            logging.warning(f"Erreur API get_candles {instrument}: {e}")
            return pd.DataFrame()

    def get_realtime_spread(self, instrument):
        try:
            params = {"instruments": instrument}
            r = pricing.PricingInfo(accountID=self.account_id, params=params)
            self.client.request(r)
            price = r.response['prices'][0]
            bid = float(price['closeoutBid'])
            ask = float(price['closeoutAsk'])
            spread_raw = ask - bid
            
            if "JPY" in instrument: pip_mult = 100
            elif ("XAU" in instrument or "XAG" in instrument or "XPT" in instrument): pip_mult = 100
            else: pip_mult = 10000
            return spread_raw, spread_raw * pip_mult
        except Exception: return 0, 0

ASSETS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD",
    "EUR_GBP", "EUR_JPY", "EUR_CHF", "EUR_CAD", "EUR_AUD", "EUR_NZD",
    "GBP_JPY", "GBP_CHF", "GBP_CAD", "GBP_AUD", "GBP_NZD",
    "AUD_JPY", "AUD_CAD", "AUD_CHF", "AUD_NZD",
    "CAD_JPY", "CAD_CHF", "NZD_JPY", "NZD_CAD", "NZD_CHF", "CHF_JPY",
    "XAU_USD", "XAG_USD", "US30_USD", "NAS100_USD", "SPX500_USD", "DE30_EUR"
]

def get_asset_params(symbol):
    if any(idx in symbol for idx in ["US30", "NAS100", "SPX500", "DE30"]):
        return {'type': 'INDEX', 'atr_threshold': 0.10, 'sl_base': 2.0, 'tp_rr': 3.0}
    if any(met in symbol for met in ["XAU", "XPT", "XAG"]):
        return {'type': 'COMMODITY', 'atr_threshold': 0.06, 'sl_base': 1.8, 'tp_rr': 2.5}
    return {'type': 'FOREX', 'atr_threshold': 0.035, 'sl_base': 1.5, 'tp_rr': 2.0}

# ==========================================
# MOTEUR D'INDICATEURS V5.1 (ADX OFFICIEL TV)
# ==========================================
class QuantEngine:
    @staticmethod
    def calculate_atr(df, period=14):
        if len(df) < period + 1: return 0
        h, l, c = df['high'], df['low'], df['close']
        tr = pd.concat([h-l, abs(h-c.shift(1)), abs(l-c.shift(1))], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean().iloc[-1]

    @staticmethod
    def calculate_adx(df, period=14):
        """
        Calcul ADX officiel TradingView (ta.rma).
        Alpha = 1/period (Wilder's Smoothing).
        """
        if len(df) < period * 2: return 0
        
        high = df['high']
        low = df['low']
        close = df['close']

        # --- dirmov(len) ---
        up = high.diff()
        down = -low.diff()

        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)

        tr = pd.concat([
            high - low, 
            abs(high - close.shift()), 
            abs(low - close.shift())
        ], axis=1).max(axis=1)

        # ta.rma correspond à Wilder's Smoothing : alpha = 1 / period
        alpha = 1 / period
        truerange = tr.ewm(alpha=alpha, adjust=False).mean()

        plus = 100 * pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean() / truerange
        minus = 100 * pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean() / truerange

        # --- adx(dilen, adxlen) ---
        sum_val = plus + minus
        
        # Gestion de la division par zéro comme dans Pine: (sum == 0 ? 1 : sum)
        dx = 100 * np.abs(plus - minus) / sum_val.replace(0, 1)

        adx = dx.ewm(alpha=alpha, adjust=False).mean()

        return adx.iloc[-1]

    @staticmethod
    def calculate_hma(series, period=20):
        if len(series) < period: return pd.Series([])
        half = int(period / 2)
        sqrt_p = int(np.sqrt(period))
        weights_half = np.arange(1, half + 1)
        weights_full = np.arange(1, period + 1)
        weights_sqrt = np.arange(1, sqrt_p + 1)
        wma_half = series.rolling(half).apply(lambda x: np.dot(x, weights_half) / weights_half.sum(), raw=True)
        wma_full = series.rolling(period).apply(lambda x: np.dot(x, weights_full) / weights_full.sum(), raw=True)
        diff = 2 * wma_half - wma_full
        hma = diff.rolling(sqrt_p).apply(lambda x: np.dot(x, weights_sqrt) / weights_sqrt.sum(), raw=True)
        return hma

    @staticmethod
    def hma_slope(hma_series, lookback=5, min_slope=0):
        if len(hma_series) < lookback + 1: return 0
        slope = (hma_series.iloc[-1] - hma_series.iloc[-1 - lookback]) / hma_series.iloc[-1]
        if slope > min_slope: return 1
        elif slope < -min_slope: return -1
        return 0

    @staticmethod
    def detect_smart_fvg(df, atr):
        if len(df) < 4: return False, 0
        curr_close = df['close'].iloc[-1]
        min_gap = atr * 0.5
        high_1 = df['high'].iloc[-3]
        low_1 = df['low'].iloc[-3]
        high_3 = df['high'].iloc[-1]
        low_3 = df['low'].iloc[-1]
        vol_mean = df['volume'].rolling(20).mean().iloc[-1]
        vol_curr = df['volume'].iloc[-1]
        
        gap_bull = low_3 - high_1
        if gap_bull > min_gap and curr_close > high_1 and vol_curr > vol_mean * 0.8: return True, "BULL"
        gap_bear = low_1 - high_3
        if gap_bear > min_gap and curr_close < low_1 and vol_curr > vol_mean * 0.8: return True, "BEAR"
        return False, None

    @staticmethod
    def get_institutional_grade(df_d, df_w):
        def analyze_tf(df):
            if len(df) < 50: return "C", "NEUTRAL", 0
            close = df['close']
            price = close.iloc[-1]
            sma200 = close.rolling(200).mean().iloc[-1] if len(df) >= 200 else close.rolling(50).mean().iloc[-1]
            ema50 = close.ewm(span=50).mean().iloc[-1]
            ema21 = close.ewm(span=21).mean().iloc[-1]
            above_sma = price > sma200
            ema50_above_sma = ema50 > sma200
            ema21_above_50 = ema21 > ema50
            price_above_21 = price > ema21
            
            if above_sma and ema50_above_sma and ema21_above_50 and price_above_21: return "A+", "BULLISH", 100
            if not above_sma and not ema50_above_sma and not ema21_above_50 and not price_above_21: return "A+", "BEARISH", 100
            if above_sma and ema50_above_sma: return "A", "BULLISH", 85
            if not above_sma and not ema50_above_sma: return "A", "BEARISH", 85
            if not above_sma and ema50_above_sma: return "B", "RETRACEMENT_BULL", 70
            if above_sma and not ema50_above_sma: return "B", "RETRACEMENT_BEAR", 70
            return "C", "NEUTRAL", 50

        grade_d, trend_d, score_d = analyze_tf(df_d)
        grade_w, trend_w, score_w = analyze_tf(df_w)
        if grade_w == "C": return "C", "NEUTRAL", 0
        final_score = (score_d * 0.6) + (score_w * 0.4)
        
        if final_score >= 95: final_grade = "A+"
        elif final_score >= 85: final_grade = "A"
        elif final_score >= 70: final_grade = "B"
        else: final_grade = "C"
        return final_grade, trend_d, final_score

    @staticmethod
    def get_midnight_open_ny(df):
        try:
            ny_tz = pytz.timezone('America/New_York')
            df_ny = df.copy()
            df_ny['time'] = pd.to_datetime(df_ny['time'], utc=True).dt.tz_convert(ny_tz)
            midnight_candle = df_ny[df_ny['time'].dt.hour == 0]
            if not midnight_candle.empty: return midnight_candle.iloc[-1]['open']
            else: return None
        except Exception: return None
    
    @staticmethod
    def get_pdh_pdl(df_d):
        if len(df_d) < 2: return None, None
        return df_d['high'].iloc[-2], df_d['low'].iloc[-2]

    @staticmethod
    def detect_structure_zscore(df, lookback=20):
        if len(df) < lookback + 1: return 0
        window = df['close'].iloc[-lookback:]
        try:
            z_score = stats.zscore(window)[-1]
            if z_score > 1.5: return 1 
            if z_score < -1.5: return -1 
        except: return 0
        return 0 

    @staticmethod
    def calculate_ha_smoothed(df, period=3):
        ha_close = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha_open = np.zeros(len(df))
        ha_open[0] = (df['open'].iloc[0] + df['close'].iloc[0]) / 2
        for i in range(1, len(df)):
            ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2
        
        smooth_open = pd.Series(ha_open).ewm(span=period).mean()
        smooth_close = ha_close.ewm(span=period).mean()
        
        if smooth_close.iloc[-1] > smooth_open.iloc[-1]: return 1 # Bull
        else: return -1 # Bear

# ==========================================
# CURRENCY STRENGTH
# ==========================================
def get_currency_strength_rsi(api):
    now = datetime.now()
    if st.session_state.cs_data.get('time') and (now - st.session_state.cs_data['time']).total_seconds() < 900:
        return st.session_state.cs_data['data']

    forex_pairs = [p for p in ASSETS if "_" in p and "XAU" not in p and "XAG" not in p and "US30" not in p]
    prices = {}
    for pair in forex_pairs[:15]: 
        try:
            df = api.get_candles(pair, "H1", 50)
            if df is not None and not df.empty: prices[pair] = df['close']
            time.sleep(0.01) 
        except Exception: continue

    if not prices: return None
    df_prices = pd.DataFrame(prices).ffill().bfill()
    
    def normalize_score(rsi_value): return ((rsi_value - 50) / 50 + 1) * 5

    def calculate_rsi_series(series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 0.0001)
        return 100 - (100 / (1 + rs))

    currencies = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]
    final_scores = {}

    for curr in currencies:
        total_score = 0.0; count = 0
        opponents = [c for c in currencies if c != curr]
        for opp in opponents:
            pair_direct = f"{curr}_{opp}"
            pair_inverse = f"{opp}_{curr}"
            rsi_val = None
            if pair_direct in df_prices.columns:
                rsi_series = calculate_rsi_series(df_prices[pair_direct])
                if not rsi_series.empty: rsi_val = rsi_series.iloc[-1]
            elif pair_inverse in df_prices.columns:
                inverted_price = 1 / df_prices[pair_inverse]
                rsi_series = calculate_rsi_series(inverted_price)
                if not rsi_series.empty: rsi_val = rsi_series.iloc[-1]
            if rsi_val is not None:
                total_score += normalize_score(rsi_val)
                count +=1
        
        if count > 0: final_scores[curr] = total_score / count
        else: final_scores[curr] = 5.0

    st.session_state.cs_data = {'data': final_scores, 'time': now}
    return final_scores

# ==========================================
# FILTRE CORRÉLATION
# ==========================================
def check_dynamic_correlation_conflict(new_signal, existing_signals, cs_scores):
    if not existing_signals: return False
    new_sym = new_signal['symbol']
    new_type = new_signal['type']
    if "_" not in new_sym: return False
    base, quote = new_sym.split('_')
    
    CORRELATION_MAP = {
        'EUR_USD':  { 'GBP_USD': 0.9, 'AUD_USD': 0.85, 'USD_CHF': -0.9, 'NZD_USD': 0.8, 'EUR_GBP': -0.8 },
        'GBP_USD':  { 'EUR_USD': 0.9, 'EUR_GBP': -0.8, 'GBP_JPY': 0.8, 'GBP_AUD': 0.7 },
        'USD_JPY':  { 'USD_CHF': 0.7, 'EUR_JPY': 0.8, 'GBP_JPY': 0.8, 'CAD_JPY': 0.7 },
        'AUD_USD':  { 'EUR_USD': 0.85, 'NZD_USD': 0.9, 'AUD_JPY': 0.8, 'AUD_NZD': 0.95 },
        'NZD_USD':  { 'AUD_USD': 0.9, 'EUR_NZD': 0.8, 'NZD_JPY': 0.8, 'AUD_NZD': 0.95 },
        'USD_CAD':  { 'USD_CHF': 0.7, 'AUD_CAD': 0.8, 'CAD_JPY': 0.7, 'EUR_CAD': 0.8 },
        'USD_CHF':  { 'EUR_USD': -0.9, 'USD_JPY': 0.7, 'USD_CAD': 0.7, 'GBP_CHF': 0.8 },
    }
    
    for existing in existing_signals:
        ex_sym = existing['symbol']
        ex_type = existing['type']
        if new_sym == ex_sym: return True 
        if new_sym in CORRELATION_MAP and ex_sym in CORRELATION_MAP[new_sym]:
            corr = CORRELATION_MAP[new_sym][ex_sym]
            if corr > 0.85 and new_type != ex_type: return True 
            if corr < -0.85 and new_type == ex_type: return True 
    return False

# ==========================================
# LOGIQUE V5.1
# ==========================================
def calculate_signal_probability_v51(df_m5, df_h1, df_h4, df_d, df_w, symbol, direction, strict_mode, spread_pips, force_open=False):
    details = {}
    rejection_reason = None
    
    atr = QuantEngine.calculate_atr(df_m5)
    atr_pct = (atr / df_m5['close'].iloc[-1]) * 100
    params = get_asset_params(symbol)
    
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    midnight_open = QuantEngine.get_midnight_open_ny(df_m5)
    adx_val = QuantEngine.calculate_adx(df_h4) # Utilisation de la fonction officielle TV
    z_score = QuantEngine.detect_structure_zscore(df_h4)
    fvg_active, fvg_type = QuantEngine.detect_smart_fvg(df_m5, atr)
    inst_grade, inst_trend, _ = QuantEngine.get_institutional_grade(df_d, df_w)
    
    curr_price = df_m5['close'].iloc[-1]
    
    # Filtre ADX Strict (ADX > 20)
    if strict_mode and adx_val < 20:
        return 0, {}, atr_pct, "ADX < 20 (Strict)"
    
    # HMA & HA M5
    hma_m5 = QuantEngine.calculate_hma(df_m5['close'], 20)
    hma_slope_m5 = QuantEngine.hma_slope(hma_m5)
    ha_status_m5 = QuantEngine.calculate_ha_smoothed(df_m5)
    
    if direction == "BUY":
        if not (hma_slope_m5 > 0 and ha_status_m5 > 0):
            return 0, {}, atr_pct, "No M5 Trigger (HMA/HA)"
    else:
        if not (hma_slope_m5 < 0 and ha_status_m5 < 0):
            return 0, {}, atr_pct, "No M5 Trigger (HMA/HA)"
            
    # Zones
    if pdh is None or midnight_open is None: return 0, {}, atr_pct, "Missing Levels"
    
    daily_range = pdh - pdl
    if daily_range == 0: daily_range = atr 
    
    if direction == "BUY":
        if curr_price > midnight_open: return 0, {}, atr_pct, "Price > Midnight"
        if curr_price > (pdl + (daily_range * 0.30)): return 0, {}, atr_pct, "Not Near PDL"
        details['zone_status'] = "DISCOUNT (Near PDL)"
    else:
        if curr_price < midnight_open: return 0, {}, atr_pct, "Price < Midnight"
        if curr_price < (pdh - (daily_range * 0.30)): return 0, {}, atr_pct, "Not Near PDH"
        details['zone_status'] = "PREMIUM (Near PDH)"
        
    # MTF Alignment
    hma_h1 = QuantEngine.calculate_hma(df_h1['close'], 20)
    slope_h1 = QuantEngine.hma_slope(hma_h1)
    
    hma_h4 = QuantEngine.calculate_hma(df_h4['close'], 20)
    slope_h4 = QuantEngine.hma_slope(hma_h4)
    
    mtf_aligned = False
    if direction == "BUY":
        if slope_h1 > 0 and slope_h4 > 0: mtf_aligned = True
        if "BULL" in inst_trend: mtf_aligned = True 
    else:
        if slope_h1 < 0 and slope_h4 < 0: mtf_aligned = True
        if "BEAR" in inst_trend: mtf_aligned = True
        
    if not mtf_aligned:
        return 0, {}, atr_pct, "MTF Misaligned"
        
    # Structure
    structure_valid = False
    if direction == "BUY":
        if fvg_active and fvg_type == "BULL": structure_valid = True
    else:
        if fvg_active and fvg_type == "BEAR": structure_valid = True
        
    if not structure_valid:
        if abs(z_score) < 1.0: structure_valid = True
        
    if not structure_valid:
        return 0, {}, atr_pct, "No Structure/FVG Support"
        
    # Scoring
    score = 0.7 
    if adx_val > 25: score += 0.1
    if abs(z_score) < 0.5: score += 0.1 
    if "A+" in inst_grade: score += 0.1
    
    details['adx_val'] = adx_val
    details['z_score'] = z_score
    details['inst_grade'] = inst_grade
    details['hma_slope'] = hma_slope_m5
    details['ha_status'] = ha_status_m5
    details['midnight'] = midnight_open
    details['pdh_pdl'] = f"{pdl:.5f}/{pdh:.5f}"
    details['fvg_active'] = fvg_active
    
    return min(score, 1.0), details, atr_pct, None

# ==========================================
# SCANNER V5.1
# ==========================================
def run_scan_v51_blue(api, min_prob, strict_mode, current_time_utc, force_open=False):
    cs_scores = get_currency_strength_rsi(api)
    signals = []
    rejected_log = []
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, sym in enumerate(ASSETS):
        progress_bar.progress((i+1)/len(ASSETS))
        status_text.markdown(f"⏳ Scan V5.1: **{sym}** ({i+1}/{len(ASSETS)})")
        
        try:
            df_d_raw = api.get_candles(sym, "D", 250)
            time.sleep(0.05)
            df_h4 = api.get_candles(sym, "H4", 100)
            time.sleep(0.05)
            df_h1 = api.get_candles(sym, "H1", 50)
            time.sleep(0.05)
            df_m5 = api.get_candles(sym, "M5", 200)
            
            if df_m5.empty or df_h1.empty or df_h4.empty or df_d_raw.empty: continue
            
            df_d = df_d_raw.iloc[-100:].copy()
            df_w = df_d_raw.set_index('time').resample('W-FRI').agg({
                'open':'first', 'high':'max', 'low':'min', 'close':'last'
            }).dropna().reset_index()
            
            spread_raw, spread_pips = api.get_realtime_spread(sym)
            
            for direction in ["BUY", "SELL"]:
                prob, details, atr_pct, reject_reason = calculate_signal_probability_v51(
                    df_m5, df_h1, df_h4, df_d, df_w, sym, direction, strict_mode, spread_pips, force_open
                )
                
                if reject_reason: 
                    rejected_log.append(f"{sym} {direction}: {reject_reason}")
                    continue
                    
                if prob < min_prob: 
                    rejected_log.append(f"{sym} {direction}: Score {prob:.2f}")
                    continue
                
                temp_signal = {'symbol': sym, 'type': direction}
                if check_dynamic_correlation_conflict(temp_signal, signals, cs_scores):
                    rejected_log.append(f"{sym} {direction}: Corrélation")
                    continue
                
                cs_aligned = False
                if "_" in sym:
                    base, quote = sym.split('_')
                    if cs_scores and base in cs_scores and quote in cs_scores:
                        gap = cs_scores.get(base, 0) - cs_scores.get(quote, 0)
                        if direction == "BUY" and gap > 0: cs_aligned = True
                        elif direction == "SELL" and gap < 0: cs_aligned = True
                
                price = df_m5['close'].iloc[-1]
                atr = QuantEngine.calculate_atr(df_m5)
                params = get_asset_params(sym)
                
                sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
                tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
                
                signals.append({
                    'symbol': sym, 'type': direction, 'price': price,
                    'prob': prob, 'score_display': prob * 10,
                    'details': details, 'atr_pct': atr_pct,
                    'sl': sl, 'tp': tp, 'rr': params['tp_rr'],
                    'cs_aligned': cs_aligned, 'spread': spread_pips
                })
                
        except Exception as e:
            logging.warning(f"Erreur scan {sym}: {e}")
            continue
            
    progress_bar.empty()
    status_text.empty()
    return sorted(signals, key=lambda x: x['prob'], reverse=True), rejected_log

# ==========================================
# AFFICHAGE
# ==========================================
def display_sig_v51(s):
    is_buy = s['type'] == 'BUY'
    col_type = "#10b981" if is_buy else "#ef4444"
    bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if is_buy else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
    
    with st.expander(f"{s['symbol']}  |  {s['type']}  |  SCORE {s['score_display']:.1f}", expanded=True):
        st.markdown(f"""
        <div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};margin-bottom:10px;">
            <span style="font-size:1.5em;font-weight:900;color:white;">{s['symbol']}</span>
            <span style="float:right;color:white;font-size:1.2em;">{s['price']:.5f}</span>
        </div>""", unsafe_allow_html=True)
        
        d = s['details']
        badges = [
            f"<span class='badge badge-blue'>HMA: {'Up' if d['hma_slope']>0 else 'Down'}</span>",
            f"<span class='badge badge-blue'>HA: {'Bull' if d['ha_status']>0 else 'Bear'}</span>",
            f"<span class='badge badge-gold'>{d['inst_grade']}</span>",
            f"<span class='badge'>ADX: {d['adx_val']:.1f}</span>"
        ]
        if s['cs_aligned']: badges.append("<span class='badge badge-session'>CS OK</span>")
        if d['fvg_active']: badges.append("<span class='badge'>FVG ACTIVE</span>")
        
        st.markdown(f"<div style='text-align:center;margin-bottom:10px'>{' '.join(badges)}</div>", unsafe_allow_html=True)
        
        c1, c2 = st.columns(2)
        c1.metric("Zone", d['zone_status'])
        c2.metric("PDH / PDL", d['pdh_pdl'])
        
        col_sl, col_tp = st.columns(2)
        col_sl.info(f"🛑 SL: {s['sl']:.5f}")
        col_tp.success(f"🎯 TP: {s['tp']:.5f}")

# ==========================================
# MAIN
# ==========================================
def main():
    st.title("🛡️ BLUESTAR ULTIMATE V5.1")
    st.markdown("<p style='text-align:center;color:#94a3b8;'>Strict ICT | ADX TV Official</p>", unsafe_allow_html=True)
    
    st.sidebar.markdown(f"🕒 UTC: {datetime.now(pytz.utc).strftime('%H:%M')}")
    
    with st.sidebar:
        st.header("⚙️ Config V5.1")
        strict_mode = st.checkbox("🔥 Strict Mode (ADX > 20)", value=True)
        min_prob = st.slider("Score Min", 60, 95, 75, 5)
        
    if st.button("🔍 Scan V5.1"):
        api = OandaClient()
        results, logs = run_scan_v51_blue(api, min_prob/100, strict_mode, datetime.now(pytz.utc))
        
        if not results:
            st.warning("Aucun signal V5.1.")
            if logs: st.write(logs)
        else:
            st.success(f"{len(results)} Signaux")
            for r in results: display_sig_v51(r)

if __name__ == "__main__":
    main()
   
