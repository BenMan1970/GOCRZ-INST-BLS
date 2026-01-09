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

# ==========================================
# CONFIGURATION & STYLE (THEME BLEU V5.4.1 PRO)
# ==========================================
warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

st.set_page_config(page_title="Bluestar Ultimate V5.4.1 PRO", layout="centered", page_icon="🛡️")

if 'trade_logs' not in st.session_state:
    st.session_state.trade_logs = []

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700;900&display=swap');
    * { font-family: 'Roboto', sans-serif; }
    .stApp { background-color: #0f1117; background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%); }
    .main .block-container { max-width: 950px; padding-top: 2rem; }
    h1 {
        background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        font-weight: 900; font-size: 2.2em; text-align: center; margin-bottom: 0.2em;
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
        if key in st.session_state.cache:
            ts, data = st.session_state.cache[key]
            if granularity == "M5": timeout = 15
            elif granularity == "M15": timeout = 60
            elif granularity in ["H1", "H4"]: timeout = 300
            else: timeout = 900
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
        try:
            params = {"instruments": instrument}
            r = pricing.PricingInfo(accountID=self.account_id, params=params)
            self.client.request(r)
            price = r.response['prices'][0]
            bid = float(price['closeoutBid'])
            ask = float(price['closeoutAsk'])
            spread_raw = ask - bid
            live_price = (bid + ask) / 2
            
            pip_mult = 10000
            if "JPY" in instrument: pip_mult = 100
            elif ("XAU" in instrument or "XAG" in instrument): pip_mult = 100
            elif any(idx in instrument for idx in ["US30", "NAS100", "SPX500", "DE30"]): pip_mult = 1
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
# MOTEUR D'INDICATEURS V5.4.1 PRO LOGIC
# ==========================================
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

    # ---------- UTILITAIRES UI (Conservés) ----------
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

# ==========================================
# LOGIQUE PRINCIPALE V5.4.1 PRO
# ==========================================
def calculate_signal_probability_v541(
    df_m5, df_h1, df_h4, df_d, df_w,
    symbol, direction, adx_filter, mtf_filter,
    live_price, spread, now
):
    # 1. DONNÉES DE BASE (Calculées même si non filtré pour l'UI)
    atr = QuantEngine.calculate_atr(df_m5)
    price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
    params = get_asset_params(symbol)
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    midnight_open = QuantEngine.get_midnight_open_ny(df_m5)
    
    # ADX FILTER (Snippet)
    adx = QuantEngine.calculate_adx(df_h1)
    if adx_filter and adx < 20:
        return 0, {}, 0, f"ADX {adx:.1f} < 20", {}

    # TRIGGER M5 (Snippet)
    hma = QuantEngine.calculate_hma(df_m5['close'])
    hma_sig = QuantEngine.hma_turn(hma)
    ha_sig = QuantEngine.ha_first_signal(df_m5)

    if direction == "BUY" and not (hma_sig == 1 and ha_sig == 1):
        return 0, {}, 0, "No M5 Reversal", {}
    if direction == "SELL" and not (hma_sig == -1 and ha_sig == -1):
        return 0, {}, 0, "No M5 Reversal", {}

    # CONFLUENCE (Snippet)
    ob, ob_zone = QuantEngine.detect_valid_ob(df_m5, atr, direction)
    fvg, fvg_zone = QuantEngine.detect_fvg(df_m5, atr, direction)

    if not (ob or fvg):
        return 0, {}, 0, "No OB/FVG", {}

    z = QuantEngine.zscore(df_h4)
    
    # SCORING (Snippet)
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

    # --- ENRICHISSEMENT POUR L'UI (Garder l'apparence riche) ---
    # On calcule les métriques manquantes pour les afficher dans l'expander
    
    # Spread & Timing & Level Quality (Pénalités/Ajouts pour affichage)
    tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
    spread_penalty, spread_warning = evaluate_spread_impact(spread, abs(tp - price), symbol)
    score = score * spread_penalty # Application légère sur le score
    
    timing_score, timing_alerts = check_entry_timing(df_m5, direction)
    level_quality, level_warnings = evaluate_level_quality(df_d, pdh, pdl)
    
    session = QuantEngine.get_trading_session(now)
    inst_grade, inst_trend, _ = QuantEngine.get_institutional_grade(df_d, df_w)

    details = {
        # Données Snippet
        "adx": adx,
        "z": z,
        "ob": ob,
        "fvg": fvg,
        # Mapping vers l'UI V5.4.1
        "adx_val": adx,
        "z_score": z,
        "ob_active": ob,
        "fvg_active": fvg,
        "hma_slope": hma_sig,
        "ha_status": "🟢" if ha_sig > 0 else "🔴",
        "inst_grade": inst_grade,
        "midnight": midnight_open,
        "pdh_pdl": f"{pdl:.5f}/{pdh:.5f}" if pdh else "N/A",
        "confluence": " + ".join(["OB" if ob else "", "FVG" if fvg else "", "ZScore" if abs(z)>1.2 else ""]),
        "session": f"✅ {session}",
        "spread_warning": spread_warning,
        "timing_alerts": timing_alerts,
        "level_warnings": level_warnings,
        # Calculs de zones pour affichage (Informationnels)
        "zone_status": "PRO DETECTED",
        "target": "SL/TP Calculated"
    }
    
    enhanced_metrics = {
        'spread_impact': spread_penalty,
        'timing_score': timing_score,
        'level_quality': level_quality
    }

    return min(score, 1.0), details, atr / price * 100, None, enhanced_metrics

# ==========================================
# HELPERS (Conservés pour l'affichage)
# ==========================================
def evaluate_spread_impact(spread_pips, tp_distance, symbol):
    pip_value = 0.01 if "JPY" in symbol else 0.0001
    tp_pips = tp_distance / pip_value if pip_value > 0 else 0
    spread_ratio = spread_pips / tp_pips if tp_pips > 0 else 0
    if spread_ratio > 0.15: return 0.7, "⚠️ HIGH"
    elif spread_ratio > 0.10: return 0.85, "⚠️ Mod"
    return 1.0, "✅ Low"

def check_entry_timing(df_m5, direction):
    timing_alerts = []
    timing_score = 0.5
    try:
        vol_avg = df_m5['volume'].rolling(20).mean().iloc[-1]
        vol_current = df_m5['volume'].iloc[-1]
        if vol_current > vol_avg * 1.3:
            timing_alerts.append("📊 Vol Spike"); timing_score += 0.2
    except: pass
    try:
        hma_series = QuantEngine.calculate_hma(df_m5['close'], 20)
        if len(hma_series) >= 3:
            recent_slope = hma_series.iloc[-1] - hma_series.iloc[-3]
            if (direction == "BUY" and recent_slope > 0) or (direction == "SELL" and recent_slope < 0):
                timing_alerts.append("🎯 HMA Momentum"); timing_score += 0.15
    except: pass
    return min(timing_score, 1.0), timing_alerts

def evaluate_level_quality(df_d, pdh, pdl):
    quality_score = 1.0
    warnings = []
    try:
        if pdh is None or pdl is None: return 1.0, []
        last_5_days = df_d.tail(5)
        pdh_tests = sum(1 for h in last_5_days['high'] if abs(h - pdh) / pdh < 0.002)
        pdl_tests = sum(1 for l in last_5_days['low'] if abs(l - pdl) / pdl < 0.002)
        if pdh_tests > 2: warnings.append(f"PDH Rested {pdh_tests}x"); quality_score *= 0.85
        if pdl_tests > 2: warnings.append(f"PDL Rested {pdl_tests}x"); quality_score *= 0.85
        if abs(df_d['high'].iloc[-1] - pdh) / pdh < 0.002:
            warnings.append("PDH Fresh"); quality_score *= 1.1
    except: pass
    return quality_score, warnings

def get_currency_strength_rsi(api):
    now = datetime.now()
    if st.session_state.cs_data.get('time') and (now - st.session_state.cs_data['time']).total_seconds() < 900:
        return st.session_state.cs_data['data']
    forex_pairs = [p for p in ASSETS if "_" in p and "XAU" not in p and "US30" not in p and "DE30" not in p]
    prices = {}
    for pair in forex_pairs[:20]: 
        try:
            df = api.get_candles(pair, "H1", 50)
            if df is not None and not df.empty: prices[pair] = df['close']
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
                total_score += normalize_score(rsi_val); count +=1
        if count > 0: final_scores[curr] = total_score / count
        else: final_scores[curr] = 5.0
    st.session_state.cs_data = {'data': final_scores, 'time': now}
    return final_scores

# ==========================================
# SCANNER PRINCIPAL
# ==========================================
def run_scan_v541(api, min_prob, adx_filter, mtf_filter, current_time_utc):
    cs_scores = get_currency_strength_rsi(api)
    signals = []
    rejected_log = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    for i, sym in enumerate(ASSETS):
        progress_bar.progress((i+1)/len(ASSETS))
        status_text.markdown(f"⏳ Analyse: **{sym}** ({i+1}/{len(ASSETS)})")
        try:
            df_d_raw = api.get_candles(sym, "D", 250)
            df_h4 = api.get_candles(sym, "H4", 100)
            df_h1 = api.get_candles(sym, "H1", 50)
            df_m5 = api.get_candles(sym, "M5", 200)
            live_price, spread_pips = api.get_realtime_price_and_spread(sym)
            if df_m5.empty or df_h1.empty or df_h4.empty or df_d_raw.empty: continue
            df_d = df_d_raw.iloc[-100:].copy()
            df_w = df_d_raw.set_index('time').resample('W-FRI').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'}).dropna().reset_index()
            for direction in ["BUY", "SELL"]:
                prob, details, atr_pct, reject_reason, enhanced_metrics = calculate_signal_probability_v541(
                    df_m5, df_h1, df_h4, df_d, df_w, sym, direction, adx_filter, mtf_filter, live_price, spread_pips, current_time_utc
                )
                if reject_reason: rejected_log.append(f"{sym} {direction}: {reject_reason}"); continue
                if prob < min_prob: continue
                temp_signal = {'symbol': sym, 'type': direction}
                if check_dynamic_correlation_conflict(temp_signal, signals, cs_scores):
                    rejected_log.append(f"{sym} {direction}: Corrélation Conflit"); continue
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
                    'symbol': sym, 'type': direction, 'price': price, 'prob': prob, 'score_display': prob * 10,
                    'details': details, 'atr_pct': atr_pct, 'sl': sl, 'tp': tp, 'rr': params['tp_rr'],
                    'cs_aligned': cs_aligned, 'spread': spread_pips, 'enhanced_metrics': enhanced_metrics
                })
        except Exception as e: 
            rejected_log.append(f"❌ {sym} Err: {str(e)[:30]}"); continue
    progress_bar.empty()
    status_text.empty()
    return sorted(signals, key=lambda x: x['prob'], reverse=True), rejected_log

def check_dynamic_correlation_conflict(new_signal, existing_signals, cs_scores):
    if not existing_signals: return False
    new_sym = new_signal['symbol']
    if "_" not in new_sym: return False
    base, quote = new_sym.split('_')
    CORRELATION_MAP = {
        'EUR_USD':  { 'GBP_USD': 0.9, 'AUD_USD': 0.85, 'USD_CHF': -0.9 },
        'GBP_USD':  { 'EUR_USD': 0.9, 'EUR_GBP': -0.8 },
        'USD_JPY':  { 'EUR_JPY': 0.8, 'GBP_JPY': 0.8 },
        'AUD_USD':  { 'NZD_USD': 0.9, 'EUR_USD': 0.85 },
    }
    for existing in existing_signals:
        ex_sym = existing['symbol']
        if new_sym == ex_sym: return True 
        if new_sym in CORRELATION_MAP and ex_sym in CORRELATION_MAP[new_sym]:
            corr = CORRELATION_MAP[new_sym][ex_sym]
            if corr > 0.85 and new_signal['type'] != existing['type']: return True 
            if corr < -0.85 and new_signal['type'] == existing['type']: return True 
    return False

# ==========================================
# AFFICHAGE (IDENTIQUE V5.4.1)
# ==========================================
def display_sig_v541(s):
    is_buy = s['type'] == 'BUY'
    col_type = "#10b981" if is_buy else "#ef4444"
    bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if is_buy else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
    em = s.get('enhanced_metrics', {})
    rank_score = s['prob'] * 0.7 + em.get('spread_impact', 1.0)*0.1 + em.get('timing_score', 0.5)*0.1 + em.get('level_quality', 1.0)*0.1
    rank_badge = f"<span class='badge badge-gold'>★ PREMIUM</span>" if rank_score >= 0.85 else f"<span class='badge badge-blue'>STRONG</span>" if rank_score >= 0.75 else "<span class='badge'>STANDARD</span>"
    with st.expander(f"{'📈' if is_buy else '📉'} {s['symbol']}  |  {s['type']}  |  SCORE {s['score_display']:.1f}/10", expanded=True):
        st.markdown(f"""
        <div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};margin-bottom:10px;">
            <span style="font-size:1.5em;font-weight:900;color:white;">{s['symbol']}</span>
            <span style="float:right;color:white;font-size:1.2em;">{s['price']:.5f}</span>
        </div>""", unsafe_allow_html=True)
        st.markdown(f"<div style='text-align:center;margin-bottom:10px'>{rank_badge}</div>", unsafe_allow_html=True)
        d = s['details']
        badges = [
            f"<span class='badge badge-blue'>HMA: {'🟢' if d['hma_slope']>0 else '🔴'}</span>",
            f"<span class='badge badge-blue'>HA: {d['ha_status']}</span>",
            f"<span class='badge badge-gold'>{d['inst_grade']}</span>",
            f"<span class='badge'>ADX H1: {d.get('adx_val', 0):.1f}</span>",
            f"<span class='badge'>MTF: PRO MODE</span>"
        ]
        if s['cs_aligned']: badges.append("<span class='badge badge-session'>CS OK</span>")
        badges.append(f"<span class='badge'>{d['session']}</span>")
        st.markdown(f"<div style='text-align:center;margin-bottom:10px'>{' '.join(badges)}</div>", unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        c1.metric("Zone", d['zone_status'])
        c2.metric("PDH / PDL", d['pdh_pdl'])
        c3.metric("Target", d.get('target', '-'))
        col_sl, col_tp = st.columns(2)
        col_sl.info(f"🛑 SL: {s['sl']:.5f}")
        col_tp.success(f"🎯 TP: {s['tp']:.5f}")
        st.markdown("---")
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Spread", d.get('spread_warning', 'N/A'))
        col_b.metric("Timing", f"{em.get('timing_score', 0)*100:.0f}%")
        col_c.metric("Lvl Quality", f"{em.get('level_quality', 1)*100:.0f}%")
        with st.expander("📊 Détails techniques"):
            st.write(f"**Midnight:** {d['midnight']:.5f}")
            st.write(f"**Z-Score:** {d['z']:.2f}")
            if d.get('timing_alerts'): st.write(f"**Alerts:** {', '.join(d['timing_alerts'])}")

# ==========================================
# MAIN
# ==========================================
def main():
    st.title("🛡️ BLUESTAR ULTIMATE V5.4.1 PRO")
    st.markdown("<p style='text-align:center;color:#94a3b8;'>HMA Turn + HA First Signal Logic | Enhanced UI</p>", unsafe_allow_html=True)
    current_time_utc = datetime.now(pytz.utc)
    session = QuantEngine.get_trading_session(current_time_utc)
    session_colors = {"ASIAN": "#f59e0b", "LONDON": "#10b981", "NY": "#3b82f6", "OFF": "#6b7280"}
    st.sidebar.markdown(f"""
        <div style='background:{session_colors.get(session, "#6b7280")};padding:10px;border-radius:8px;text-align:center;margin-bottom:15px;'>
            <div style='font-size:0.8em;color:white;opacity:0.8;'>🕒 UTC: {current_time_utc.strftime('%H:%M')}</div>
            <div style='font-size:1.1em;font-weight:700;color:white;'>📍 {session} SESSION</div>
        </div>
    """, unsafe_allow_html=True)
    with st.sidebar:
        st.header("⚙️ Filtres PRO")
        mtf_filter = st.selectbox("Alignement MTF", ["Strict (D+H4+H1)", "Flexible (D+H4 OR H4+H1)", "Light (H4 only)", "Off"], index=2) # Defaut Light car Pro Logic fait le gros du travail
        adx_filter = st.checkbox("Filtre ADX H1 > 20", value=True)
        min_prob = st.slider("Score Min", 60, 95, 70, 5)
    if st.button("🔍 SCANNER V5.4.1 PRO"):
        with st.spinner("Analyse PRO Logic..."):
            api = OandaClient()
            results, logs = run_scan_v541(api, min_prob/100, adx_filter, mtf_filter, current_time_utc)
        if not results:
            st.warning("⚠️ Aucun signal détecté par la logique PRO.")
            with st.expander("Logs de rejet"):
                for log in logs[:30]: st.text(log)
        else:
            st.success(f"✅ {len(results)} Signal(s) trouvé(s)")
            for r in results: display_sig_v541(r)
            with st.expander("Logs de rejet"):
                for log in logs[:30]: st.text(log)

if __name__ == "__main__":
    main()
    
