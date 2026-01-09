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
import sys
from datetime import datetime, timedelta
from scipy import stats 
import pytz
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
# CONFIGURATION & STYLE (THEME BLEU V5.3)
# ==========================================
warnings.simplefilter(action='ignore', category=FutureWarning)

# Supprimer les logs système de Streamlit
logging.getLogger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

st.set_page_config(page_title="Bluestar Ultimate V5.3", layout="centered", page_icon="🛡️")

LOG_FILE = "bluestar_v53_log.csv"

if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            "timestamp", "symbol", "direction", "price", "score", "hma_m5", "ha_status", 
            "pdh_pdl_status", "fvg_status", "mtf_strict", "adx_m5", "sl", "tp"
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
        if key in st.session_state.cache and granularity in ["H4", "D"]:
            ts, data = st.session_state.cache[key]
            timeout = 300 if granularity in ["H4", "D"] else 30
            if (datetime.now() - ts).total_seconds() < timeout: return data

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
            return pd.DataFrame()

    def get_realtime_price_and_spread(self, instrument):
        """Récupère le prix live (Bid/Ask) pour éviter la latence des bougies fermées"""
        try:
            params = {"instruments": instrument}
            r = pricing.PricingInfo(accountID=self.account_id, params=params)
            self.client.request(r)
            price = r.response['prices'][0]
            bid = float(price['closeoutBid'])
            ask = float(price['closeoutAsk'])
            spread_raw = ask - bid
            live_price = (bid + ask) / 2
            
            if "JPY" in instrument: pip_mult = 100
            elif ("XAU" in instrument or "XAG" in instrument or "XPT" in instrument): pip_mult = 100
            else: pip_mult = 10000
            
            return live_price, spread_raw * pip_mult
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
# MOTEUR D'INDICATEURS V5.3
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
        if len(df) < period * 2: return 0
        high, low, close = df['high'], df['low'], df['close']
        up = high.diff()
        down = -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
        alpha = 1 / period
        truerange = tr.ewm(alpha=alpha, adjust=False).mean()
        plus = 100 * pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean() / truerange
        minus = 100 * pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean() / truerange
        sum_val = plus + minus
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
        if len(df) < 4: return False, None, None
        curr_close = df['close'].iloc[-1]
        min_gap = atr * 0.5
        high_1 = df['high'].iloc[-3]
        low_1 = df['low'].iloc[-3]
        high_3 = df['high'].iloc[-1]
        low_3 = df['low'].iloc[-1]
        vol_mean = df['volume'].rolling(20).mean().iloc[-1]
        vol_curr = df['volume'].iloc[-1]
        
        gap_bull = low_3 - high_1
        if gap_bull > min_gap and curr_close > high_1 and vol_curr > vol_mean * 0.8: 
            return True, "BULL", (high_1, low_3)
        gap_bear = low_1 - high_3
        if gap_bear > min_gap and curr_close < low_1 and vol_curr > vol_mean * 0.8: 
            return True, "BEAR", (high_3, low_1)
        return False, None, None

    @staticmethod
    def detect_order_block(df, atr, direction):
        if len(df) < 6: return False, None
        
        for i in range(-5, -1):
            candle_body = abs(df['close'].iloc[i] - df['open'].iloc[i])
            impulse_body = abs(df['close'].iloc[i+1] - df['open'].iloc[i+1])
            is_significant = impulse_body > (candle_body * 1.5)
            
            if direction == "BUY":
                is_bearish = df['close'].iloc[i] < df['open'].iloc[i]
                strong_rally = df['close'].iloc[i+1] > df['close'].iloc[i] + (atr * 0.3)
                if is_bearish and strong_rally and is_significant:
                    ob_zone = (df['low'].iloc[i], df['high'].iloc[i])
                    return True, ob_zone
                    
            else:  # SELL
                is_bullish = df['close'].iloc[i] > df['open'].iloc[i]
                strong_drop = df['close'].iloc[i+1] < df['close'].iloc[i] - (atr * 0.3)
                if is_bullish and strong_drop and is_significant:
                    ob_zone = (df['low'].iloc[i], df['high'].iloc[i])
                    return True, ob_zone
                    
        return False, None

    @staticmethod
    def is_price_in_zone(price, zone):
        if zone is None: return False
        return zone[0] <= price <= zone[1]

    @staticmethod
    def get_trading_session(current_time_utc):
        hour = current_time_utc.hour
        if 23 <= hour or hour < 8: return "ASIAN"
        elif 8 <= hour < 16: return "LONDON"
        elif 13 <= hour < 21: return "NY"
        elif 8 <= hour < 13: return "LONDON"
        else: return "OFF"

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
            return z_score
        except: 
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
        
        if smooth_close.iloc[-1] > smooth_open.iloc[-1]: return 1
        else: return -1

# ==========================================
# CURRENCY STRENGTH
# ==========================================
def get_currency_strength_rsi(api):
    now = datetime.now()
    if st.session_state.cs_data.get('time') and (now - st.session_state.cs_data['time']).total_seconds() < 900:
        return st.session_state.cs_data['data']

    forex_pairs = [p for p in ASSETS if "_" in p and "XAU" not in p and "XAG" not in p and "US30" not in p and "NAS100" not in p and "SPX500" not in p and "DE30" not in p]
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
# LOGIQUE V5.3 (STRICT MTF + ADX H1 ONLY)
# ==========================================
def calculate_signal_probability_v53(df_m5, df_h1, df_h4, df_d, df_w, symbol, direction, strict_mode, adx_filter, mtf_filter, live_price_raw, spread_pips, current_time_utc, force_open=False):
    details = {}
    rejection_reason = None
    
    atr = QuantEngine.calculate_atr(df_m5)
    
    # Utilisation du prix LIVE
    curr_price = live_price_raw if live_price_raw > 0 else df_m5['close'].iloc[-1]
    
    atr_pct = (atr / curr_price) * 100
    params = get_asset_params(symbol)
    
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    midnight_open = QuantEngine.get_midnight_open_ny(df_m5)
    
    # Calcul ADX H1 et M5
    adx_m5 = QuantEngine.calculate_adx(df_m5)
    adx_h1 = QuantEngine.calculate_adx(df_h1)
    
    z_score = QuantEngine.detect_structure_zscore(df_h4)
    fvg_active, fvg_type, fvg_zone = QuantEngine.detect_smart_fvg(df_m5, atr)
    ob_active, ob_zone = QuantEngine.detect_order_block(df_m5, atr, direction)
    inst_grade, inst_trend, _ = QuantEngine.get_institutional_grade(df_d, df_w)
    
    # SESSION FILTER
    session = QuantEngine.get_trading_session(current_time_utc)
    if session == "ASIAN":
        details['session_warning'] = "⚠️ ASIAN (Low liquidity)"
    elif session in ["LONDON", "NY"]:
        details['session'] = f"✅ {session}"
    
    # Filtre ADX: UNIQUEMENT SUR H1
    if adx_filter:
        if adx_h1 < 20:
             return 0, {}, atr_pct, f"ADX H1 {adx_h1:.1f} < 20 (Weak Trend)"
    
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
            
    # Zones (Utilisation du prix LIVE)
    if pdh is None or midnight_open is None: 
        return 0, {}, atr_pct, "Missing Levels"
    
    daily_range = pdh - pdl
    if daily_range == 0: daily_range = atr 
    
    if direction == "BUY":
        if curr_price > midnight_open: 
            return 0, {}, atr_pct, "Price > Midnight (Need Below for BUY)"
        if curr_price > (pdl + (daily_range * 0.40)): 
            return 0, {}, atr_pct, "Not in Discount Zone (Far from PDL)"
        details['zone_status'] = "DISCOUNT (Below Midnight → PDH)"
        details['target'] = f"PDH: {pdh:.5f}"
    else:
        if curr_price < midnight_open: 
            return 0, {}, atr_pct, "Price < Midnight (Need Above for SELL)"
        if curr_price < (pdh - (daily_range * 0.40)): 
            return 0, {}, atr_pct, "Not in Premium Zone (Far from PDH)"
        details['zone_status'] = "PREMIUM (Above Midnight → PDL)"
        details['target'] = f"PDL: {pdl:.5f}"
        
    # MTF ALIGNEMENT
    hma_h1 = QuantEngine.calculate_hma(df_h1['close'], 20)
    slope_h1 = QuantEngine.hma_slope(hma_h1)
    
    hma_h4 = QuantEngine.calculate_hma(df_h4['close'], 20)
    slope_h4 = QuantEngine.hma_slope(hma_h4)
    
    mtf_aligned = False
    
    if mtf_filter == "Strict (D+H4+H1)":
        if direction == "BUY":
            if slope_h1 > 0 and slope_h4 > 0 and "BULL" in inst_trend: mtf_aligned = True
        else:
            if slope_h1 < 0 and slope_h4 < 0 and "BEAR" in inst_trend: mtf_aligned = True
    
    elif mtf_filter == "Flexible (D+H4 OR H4+H1)":
        if direction == "BUY":
            condition1 = slope_h4 > 0 and "BULL" in inst_trend
            condition2 = slope_h1 > 0 and slope_h4 > 0
            if condition1 or condition2: mtf_aligned = True
        else:
            condition1 = slope_h4 < 0 and "BEAR" in inst_trend
            condition2 = slope_h1 < 0 and slope_h4 < 0
            if condition1 or condition2: mtf_aligned = True
    
    elif mtf_filter == "Light (H4 only)":
        if direction == "BUY":
            if slope_h4 > 0: mtf_aligned = True
        else:
            if slope_h4 < 0: mtf_aligned = True
    
    elif mtf_filter == "Off":
        mtf_aligned = True
        
    if not mtf_aligned:
        return 0, {}, atr_pct, f"MTF Misaligned ({mtf_filter})"
        
    # CONFLUENCE: FVG OU OB OU Support/Resistance
    confluence_found = False
    confluence_type = []
    
    # Vérifier FVG
    if fvg_active:
        price_in_fvg = QuantEngine.is_price_in_zone(curr_price, fvg_zone)
        if direction == "BUY" and fvg_type == "BULL" and price_in_fvg:
            confluence_found = True
            confluence_type.append("FVG")
        elif direction == "SELL" and fvg_type == "BEAR" and price_in_fvg:
            confluence_found = True
            confluence_type.append("FVG")
    
    # Vérifier Order Block
    if ob_active:
        price_in_ob = QuantEngine.is_price_in_zone(curr_price, ob_zone)
        if price_in_ob:
            confluence_found = True
            confluence_type.append("OB")
    
    # Fallback: Z-score
    if not confluence_found:
        if direction == "BUY" and z_score < -1.0:
            confluence_found = True
            confluence_type.append("OVERSOLD")
        elif direction == "SELL" and z_score > 1.0:
            confluence_found = True
            confluence_type.append("OVERBOUGHT")
        
    if not confluence_found:
        return 0, {}, atr_pct, "No Confluence (Need FVG/OB/Support)"
        
    # Scoring
    score = 0.70
    
    if adx_h1 > 25: score += 0.10
    if adx_m5 > 30: score += 0.05
    
    if len(confluence_type) > 1: score += 0.05
    
    if "A+" in inst_grade: score += 0.10
    elif "A" in inst_grade: score += 0.05
    
    if session in ["LONDON", "NY"]: score += 0.05
    
    details['adx_val'] = adx_m5
    details['z_score'] = z_score
    details['inst_grade'] = inst_grade
    details['hma_slope'] = hma_slope_m5
    details['ha_status'] = ha_status_m5
    details['midnight'] = midnight_open
    details['pdh_pdl'] = f"{pdl:.5f}/{pdh:.5f}"
    details['confluence'] = " + ".join(confluence_type)
    details['fvg_active'] = fvg_active
    details['ob_active'] = ob_active
    details['mtf_mode'] = mtf_filter
    
    return min(score, 1.0), details, atr_pct, None

# ==========================================
# FONCTION HELPER POUR ASYNC
# ==========================================
def fetch_asset_data(api, sym):
    try:
        df_d_raw = api.get_candles(sym, "D", 250)
        df_h4 = api.get_candles(sym, "H4", 100)
        df_h1 = api.get_candles(sym, "H1", 50)
        df_m5 = api.get_candles(sym, "M5", 200)
        live_price, spread_pips = api.get_realtime_price_and_spread(sym)
        return sym, df_d_raw, df_h4, df_h1, df_m5, live_price, spread_pips
    except Exception as e:
        return sym, None, None, None, None, 0, 0

# ==========================================
# SCANNER V5.3 (PARALLELE)
# ==========================================
def run_scan_v53_blue(api, min_prob, adx_filter, mtf_filter, current_time_utc, force_open=False):
    cs_scores = get_currency_strength_rsi(api)
    signals = []
    rejected_log = []
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    status_text.markdown(f"⏳ **Initialisation du scan parallèle...**")
    
    asset_data = {}
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_asset_data, api, sym): sym for sym in ASSETS}
        completed_count = 0
        for future in as_completed(futures):
            sym, df_d_raw, df_h4, df_h1, df_m5, live_price, spread_pips = future.result()
            if df_m5 is not None and not df_m5.empty:
                asset_data[sym] = (df_d_raw, df_h4, df_h1, df_m5, live_price, spread_pips)
            completed_count += 1
            progress_bar.progress(completed_count / len(ASSETS))
            status_text.markdown(f"⏳ Données reçues: {sym} ({completed_count}/{len(ASSETS)})")

    status_text.markdown("⏳ Analyse technique en cours...")
    
    for sym in asset_data:
        df_d_raw, df_h4, df_h1, df_m5, live_price, spread_pips = asset_data[sym]
        
        if df_m5.empty or df_h1.empty or df_h4.empty or df_d_raw.empty: 
            continue
        
        df_d = df_d_raw.iloc[-100:].copy()
        df_w = df_d_raw.set_index('time').resample('W-FRI').agg({
            'open':'first', 'high':'max', 'low':'min', 'close':'last'
        }).dropna().reset_index()
        
        for direction in ["BUY", "SELL"]:
            prob, details, atr_pct, reject_reason = calculate_signal_probability_v53(
                df_m5, df_h1, df_h4, df_d, df_w, sym, direction, None, adx_filter, mtf_filter, live_price, spread_pips, current_time_utc, force_open
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
            
            price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
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
            
    progress_bar.empty()
    status_text.empty()
    return sorted(signals, key=lambda x: x['prob'], reverse=True), rejected_log

# ==========================================
# AFFICHAGE
# ==========================================
def display_sig_v53(s):
    is_buy = s['type'] == 'BUY'
    col_type = "#10b981" if is_buy else "#ef4444"
    bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if is_buy else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
    
    with st.expander(f"{'📈' if is_buy else '📉'} {s['symbol']}  |  {s['type']}  |  SCORE {s['score_display']:.1f}/10", expanded=True):
        st.markdown(f"""
        <div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};margin-bottom:10px;">
            <span style="font-size:1.5em;font-weight:900;color:white;">{s['symbol']}</span>
            <span style="float:right;color:white;font-size:1.2em;">{s['price']:.5f}</span>
        </div>""", unsafe_allow_html=True)
        
        d = s['details']
        
        badges = [
            f"<span class='badge badge-blue'>HMA: {'🟢 Up' if d['hma_slope']>0 else '🔴 Down'}</span>",
            f"<span class='badge badge-blue'>HA: {'🟢 Bull' if d['ha_status']>0 else '🔴 Bear'}</span>",
            f"<span class='badge badge-gold'>{d['inst_grade']}</span>",
            f"<span class='badge'>ADX M5: {d['adx_val']:.1f}</span>",
            f"<span class='badge'>MTF: {d.get('mtf_mode', 'N/A')}</span>"
        ]
        
        if 'confluence' in d:
            badges.append(f"<span class='badge badge-session'>🎯 {d['confluence']}</span>")
        
        if s['cs_aligned']: 
            badges.append("<span class='badge badge-session'>💪 CS OK</span>")
        
        if 'session' in d:
            badges.append(f"<span class='badge'>{d['session']}</span>")
        elif 'session_warning' in d:
            badges.append(f"<span class='badge' style='background:#f59e0b;color:black;'>{d['session_warning']}</span>")
        
        st.markdown(f"<div style='text-align:center;margin-bottom:10px'>{' '.join(badges)}</div>", unsafe_allow_html=True)
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Zone", d['zone_status'])
        c2.metric("PDH / PDL", d['pdh_pdl'])
        if 'target' in d:
            c3.metric("Target", d['target'])
        
        col_sl, col_tp = st.columns(2)
        col_sl.info(f"🛑 SL: {s['sl']:.5f} (R:{s['rr']:.1f})")
        col_tp.success(f"🎯 TP: {s['tp']:.5f}")
        
        with st.expander("📊 Détails techniques"):
            st.write(f"**Midnight Open:** {d['midnight']:.5f}")
            st.write(f"**Z-Score H4:** {d['z_score']:.2f}")
            st.write(f"**Spread:** {s['spread']:.1f} pips")
            st.write(f"**ATR %:** {s['atr_pct']:.3f}%")
            if d.get('fvg_active'):
                st.success("✅ FVG détecté")
            if d.get('ob_active'):
                st.success("✅ Order Block détecté")

# ==========================================
# MAIN
# ==========================================
def main():
    st.title("🛡️ BLUESTAR ULTIMATE V5.3")
    st.markdown("<p style='text-align:center;color:#94a3b8;'>Scanner institutionnel | MTF Flexible | OB + FVG Detection</p>", unsafe_allow_html=True)
    
    current_time_utc = datetime.now(pytz.utc)
    session = QuantEngine.get_trading_session(current_time_utc)
    
    session_colors = {
        "ASIAN": "#f59e0b",
        "LONDON": "#10b981", 
        "NY": "#3b82f6",
        "OFF": "#6b7280"
    }
    session_color = session_colors.get(session, "#6b7280")
    
    st.sidebar.markdown(f"""
        <div style='background:{session_color};padding:10px;border-radius:8px;text-align:center;margin-bottom:15px;'>
            <div style='font-size:0.8em;color:white;opacity:0.8;'>🕒 UTC: {current_time_utc.strftime('%H:%M')}</div>
            <div style='font-size:1.1em;font-weight:700;color:white;'>📍 {session} SESSION</div>
        </div>
    """, unsafe_allow_html=True)
    
    with st.sidebar:
        st.header("⚙️ Filtres")
        
        mtf_filter = st.selectbox(
            "🎯 Alignement Multi-Timeframe",
            ["Strict (D+H4+H1)", "Flexible (D+H4 OR H4+H1)", "Light (H4 only)", "Off"],
            index=0,
            help="Strict = tous alignés | Flexible = 2 sur 3 | Light = H4 seul | Off = pas de filtre"
        )
        
        # MODIFICATION DU LABEL ICI
        adx_filter = st.checkbox(
            "🔥 Filtre ADX", 
            value=True,
            help="Exige ADX supérieur à 20 sur H1 pour confirmer la tendance de fond"
        )
        
        min_prob = st.slider("Score Minimum", 60, 95, 75, 5)
        
    if st.button("🔍 LANCER LE SCAN V5.3"):
        with st.spinner("🔄 Scan en cours..."):
            api = OandaClient()
            results, logs = run_scan_v53_blue(api, min_prob/100, adx_filter, mtf_filter, current_time_utc)
        
        if not results:
            st.warning("⚠️ Aucun signal trouvé avec les critères actuels.")
            st.info("""
            **💡 Suggestions pour obtenir plus de signaux:**
            - Essayer mode MTF **Flexible** ou **Light**
            - Désactiver le filtre ADX temporairement
            - Réduire le score minimum à 70
            """)
            with st.expander("📋 Logs de rejet (pourquoi pas de signaux)"):
                for log in logs[:25]: 
                    st.text(log)
        else:
            st.success(f"✅ **{len(results)} Signal(s) détecté(s)** - Vérifiez sur vos graphiques !")
            st.info(f"🎯 **Filtre actif:** MTF = {mtf_filter} | ADX = {'ON (H1)' if adx_filter else 'OFF'}")
            
            for r in results: 
                display_sig_v53(r)
            
            with st.expander("📋 Voir tous les rejets"):
                for log in logs:
                    st.text(log)

if __name__ == "__main__":
    main()
