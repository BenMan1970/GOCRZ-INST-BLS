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

st.set_page_config(page_title="Bluestar Ultimate V5.3 Enhanced", layout="centered", page_icon="🛡️")

LOG_FILE = "bluestar_v53_log.csv"

if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            "timestamp", "symbol", "direction", "price", "score", "hma_m5", "ha_status", 
            "pdh_pdl_status", "fvg_status", "mtf_strict", "adx_m5", "sl", "tp", "rank_score"
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
    .badge-rank { background: linear-gradient(135deg, #10b981 0%, #059669 100%); font-size: 0.85em; }
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
        if len(df) < 4: return False, None, None, 0
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
            return True, "BULL", (high_1, low_3), 1  # Age = 1 (récent)
        gap_bear = low_1 - high_3
        if gap_bear > min_gap and curr_close < low_1 and vol_curr > vol_mean * 0.8: 
            return True, "BEAR", (high_3, low_1), 1
        return False, None, None, 0

    @staticmethod
    def detect_order_block(df, atr, direction):
        if len(df) < 6: return False, None, 0
        
        for i in range(-5, -1):
            candle_body = abs(df['close'].iloc[i] - df['open'].iloc[i])
            impulse_body = abs(df['close'].iloc[i+1] - df['open'].iloc[i+1])
            is_significant = impulse_body > (candle_body * 1.5)
            ob_age = abs(i)
            
            if direction == "BUY":
                is_bearish = df['close'].iloc[i] < df['open'].iloc[i]
                strong_rally = df['close'].iloc[i+1] > df['close'].iloc[i] + (atr * 0.3)
                if is_bearish and strong_rally and is_significant:
                    ob_zone = (df['low'].iloc[i], df['high'].iloc[i])
                    return True, ob_zone, ob_age
                    
            else:  # SELL
                is_bullish = df['close'].iloc[i] > df['open'].iloc[i]
                strong_drop = df['close'].iloc[i+1] < df['close'].iloc[i] - (atr * 0.3)
                if is_bullish and strong_drop and is_significant:
                    ob_zone = (df['low'].iloc[i], df['high'].iloc[i])
                    return True, ob_zone, ob_age
                    
        return False, None, 0

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
# NOUVEAUX TWEAKS PRO
# ==========================================

def calculate_confluence_quality(signal, df_m5, fvg_zone, ob_zone, fvg_age, ob_age):
    """Score la QUALITÉ de chaque élément de confluence"""
    quality_score = 0
    weights = {}
    
    price = signal['price']
    
    # FVG Quality
    if signal['details'].get('fvg_active') and fvg_zone:
        fvg_center = (fvg_zone[0] + fvg_zone[1]) / 2
        distance_ratio = abs(price - fvg_center) / abs(fvg_zone[1] - fvg_zone[0])
        fvg_quality = max(0, 1 - distance_ratio)
        quality_score += fvg_quality * 0.20
        weights['fvg_q'] = f"{fvg_quality:.2f}"
    
    # Order Block Quality
    if signal['details'].get('ob_active') and ob_zone:
        ob_quality = max(0, 1 - (ob_age / 20))
        quality_score += ob_quality * 0.20
        weights['ob_q'] = f"{ob_quality:.2f}"
    
    # ADX Quality (progressive)
    adx = signal['details']['adx_val']
    if adx >= 20:
        adx_quality = min(1.0, (adx - 20) / 30)
        quality_score += adx_quality * 0.15
        weights['adx_q'] = f"{adx_quality:.2f}"
    
    # Zone Quality (distance à PDL/PDH)
    pdh_pdl_str = signal['details']['pdh_pdl']
    if '/' in pdh_pdl_str:
        pdl = float(pdh_pdl_str.split('/')[0])
        pdh = float(pdh_pdl_str.split('/')[1])
        
        if 'DISCOUNT' in signal['details']['zone_status']:
            distance_to_pdl = abs(price - pdl) / price
            zone_quality = max(0, 1 - (distance_to_pdl * 100))
            quality_score += zone_quality * 0.25
            weights['zone_q'] = f"{zone_quality:.2f}"
        elif 'PREMIUM' in signal['details']['zone_status']:
            distance_to_pdh = abs(price - pdh) / price
            zone_quality = max(0, 1 - (distance_to_pdh * 100))
            quality_score += zone_quality * 0.25
            weights['zone_q'] = f"{zone_quality:.2f}"
    
    # MTF Strength
    hma_h1_slope = signal['details'].get('hma_h1_slope', 0)
    hma_h4_slope = signal['details'].get('hma_h4_slope', 0)
    mtf_strength = (abs(hma_h1_slope) + abs(hma_h4_slope)) / 2
    quality_score += min(mtf_strength * 10, 0.20)
    weights['mtf_str'] = f"{mtf_strength:.3f}"
    
    return quality_score, weights


def detect_optimal_entry_window(df_m5, signal):
    """Identifie si on est dans la fenêtre idéale d'entrée"""
    timing_score = 0
    alerts = []
    
    # HMA juste passé au vert
    hma_series = QuantEngine.calculate_hma(df_m5['close'], 20)
    if len(hma_series) >= 5:
        for i in range(-3, 0):
            if signal['type'] == 'BUY':
                if hma_series.iloc[i-1] < hma_series.iloc[i-2] and hma_series.iloc[i] > hma_series.iloc[i-1]:
                    alerts.append("🎯 HMA Fresh Cross")
                    timing_score += 0.3
                    break
            else:
                if hma_series.iloc[i-1] > hma_series.iloc[i-2] and hma_series.iloc[i] < hma_series.iloc[i-1]:
                    alerts.append("🎯 HMA Fresh Cross")
                    timing_score += 0.3
                    break
    
    # HA vient de changer
    ha_changes = []
    for i in range(-5, -1):
        ha_curr = QuantEngine.calculate_ha_smoothed(df_m5.iloc[:i+1])
        ha_changes.append(ha_curr)
    
    if len(ha_changes) >= 3:
        if signal['type'] == 'BUY' and ha_changes[-1] > 0 and ha_changes[-2] <= 0:
            alerts.append("⚡ HA Just Green")
            timing_score += 0.3
        elif signal['type'] == 'SELL' and ha_changes[-1] < 0 and ha_changes[-2] >= 0:
            alerts.append("⚡ HA Just Red")
            timing_score += 0.3
    
    # Volume spike
    vol_avg = df_m5['volume'].rolling(20).mean().iloc[-1]
    vol_current = df_m5['volume'].iloc[-1]
    if vol_current > vol_avg * 1.3:
        alerts.append("📊 Vol Spike")
        timing_score += 0.2
    
    return min(timing_score, 1.0), alerts


def adjust_for_execution_costs(signal):
    """Downgrade le signal si spread mange le profit potentiel"""
    spread_pips = signal['spread']
    distance_to_tp = abs(signal['tp'] - signal['price'])
    
    if "JPY" in signal['symbol']:
        pip_value = 0.01
    elif any(x in signal['symbol'] for x in ['XAU', 'XAG']):
        pip_value = 0.01
    else:
        pip_value = 0.0001
    
    tp_pips = distance_to_tp / pip_value
    spread_ratio = spread_pips / tp_pips if tp_pips > 0 else 0
    
    if spread_ratio > 0.15:
        signal['spread_warning'] = f"⚠️ HIGH ({spread_ratio*100:.1f}%)"
        signal['adjusted_score'] = signal['prob'] * 0.7
    elif spread_ratio > 0.10:
        signal['spread_warning'] = f"⚠️ Mod ({spread_ratio*100:.1f}%)"
        signal['adjusted_score'] = signal['prob'] * 0.85
    else:
        signal['spread_warning'] = f"✅ Low ({spread_ratio*100:.1f}%)"
        signal['adjusted_score'] = signal['prob']
    
    signal['spread_ratio'] = spread_ratio
    return signal


def evaluate_level_freshness(df_d, pdh, pdl):
    """PDH/PDL récemment testé = moins fiable"""
    freshness_score = 1.0
    warnings = []
    
    last_5_days = df_d.tail(5)
    pdh_tests = sum(1 for h in last_5_days['high'] if abs(h - pdh) / pdh < 0.002)
    pdl_tests = sum(1 for l in last_5_days['low'] if abs(l - pdl) / pdl < 0.002)
    
    if pdh_tests > 2:
        warnings.append(f"PDH tested {pdh_tests}x")
        freshness_score *= 0.8
    
    if pdl_tests > 2:
        warnings.append(f"PDL tested {pdl_tests}x")
        freshness_score *= 0.8
    
    for i in range(-1, -6, -1):
        if abs(df_d['high'].iloc[i] - pdh) / pdh < 0.002:
            days_since = abs(i)
            if days_since == 1:
                warnings.append("PDH Fresh")
                freshness_score *= 1.1
            break
    
    return freshness_score, warnings


def add_contextual_metadata(signal, df_d, df_h4):
    """Info contextuelle pour décision manuelle"""
    metadata = {}
    
    # Tendance D
    sma_50_d = df_d['close'].rolling(50).mean().iloc[-1]
    price_vs_sma = ((signal['price'] - sma_50_d) / sma_50_d) * 100
    metadata['d_trend'] = f"{'📈' if price_vs_sma > 0 else '📉'} {price_vs_sma:+.2f}% vs SMA50"
    
    # Dernière bougie H4
    last_h4 = df_h4.iloc[-1]
    h4_candle_size = abs(last_h4['close'] - last_h4['open'])
    h4_atr = QuantEngine.calculate_atr(df_h4)
    candle_strength = h4_candle_size / h4_atr if h4_atr > 0 else 0
    
    if candle_strength > 1.5:
        metadata['h4_candle'] = f"🔥 Strong ({'Bull' if last_h4['close'] > last_h4['open'] else 'Bear'}) {candle_strength:.1f}x"
    else:
        metadata['h4_candle'] = f"Weak H4 {candle_strength:.1f}x"
    
    # Volatilité
    current_atr = signal['atr_pct']
    avg_atr_20 = df_d['close'].pct_change().abs().rolling(20).mean().iloc[-1] * 100
    vol_ratio = current_atr / avg_atr_20 if avg_atr_20 > 0 else 1
    
    if vol_ratio > 1.3:
        metadata['volatility'] = f"⚡ High Vol {vol_ratio:.1f}x"
    elif vol_ratio < 0.7:
        metadata['volatility'] = f"😴 Low Vol {vol_ratio:.1f}x"
    else:
        metadata['volatility'] = f"✅ Normal {vol_ratio:.1f}x"
    
    return metadata


def calculate_final_rank(signals):
    """Combine tous les scores pour ranking intelligent"""
    for sig in signals:
        base_score = sig.get('adjusted_score', sig['prob'])
        confluence_quality = sig.get('confluence_quality', 0.5)
        timing_score = sig.get('timing_score', 0.5)
        freshness = sig.get('freshness_score', 1.0)
        spread_penalty = 1.0 - sig.get('spread_ratio', 0)
        
        final_score = (
            base_score * 0.35 +
            confluence_quality * 0.25 +
            timing_score * 0.20 +
            freshness * 0.10 +
            spread_penalty * 0.10
        )
        
        sig['final_rank_score'] = final_score
        sig['rank_breakdown'] = {
            'base': f"{base_score:.2f}",
            'confl_q': f"{confluence_quality:.2f}",
            'timing': f"{timing_score:.2f}",
            'fresh': f"{freshness:.2f}",
            'spread': f"{spread_penalty:.2f}"
        }
    
    return sorted(signals, key=lambda x: x['final_rank_score'], reverse=True)
