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
# CONFIGURATION & STYLE (THEME BLEU V6.2)
# ==========================================
warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

st.set_page_config(page_title="Bluestar Ultimate V6.2", layout="centered", page_icon="🛡️")

if 'trade_logs' not in st.session_state:
    st.session_state.trade_logs = []
if 'active_zones' not in st.session_state:
    st.session_state.active_zones = {} # Track used zones: {symbol: {'zone': (low, high), 'dir': 'BUY'}}

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
    .badge-fail { background: #ef4444; }
    .badge-strict { background: #8b5cf6; } 
    .badge-asian { background: #64748b; }
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
            elif granularity == "D": timeout = 900
            elif granularity == "W": timeout = 3600 
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
# MOTEUR D'INDICATEURS V6.2 LOGIC
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

    # ---------- ADX & DI (Directionnel) ----------
    @staticmethod
    def calculate_adx_and_di(df, period=14):
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
        adx = dx.ewm(alpha=1/period).mean().iloc[-1]
        # Retourne l'ADX et la direction (1 si +DM > -DM, -1 si -DM > +DM)
        direction = 1 if plus_di.iloc[-1] > minus_di.iloc[-1] else -1
        return adx, direction

    # ---------- HMA ----------
    @staticmethod
    def calculate_hma(series, period=20):
        half = int(period / 2)
        sqrt = int(np.sqrt(period))
        wma_half = series.rolling(half).apply(lambda x: np.dot(x, np.arange(1, half+1)) / np.arange(1, half+1).sum(), raw=True)
        wma_full = series.rolling(period).apply(lambda x: np.dot(x, np.arange(1, period+1)) / np.arange(1, period+1).sum(), raw=True)
        diff = 2 * wma_half - wma_full
        return diff.rolling(sqrt).apply(lambda x: np.dot(x, np.arange(1, sqrt+1)) / np.arange(1, sqrt+1).sum(), raw=True)

    # ---------- HEIKEN ASHI OHLC ----------
    @staticmethod
    def get_ha_ohlc(df):
        ha_close = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha_open = ha_close.copy()
        ha_open.iloc[0] = (df['open'].iloc[0] + df['close'].iloc[0]) / 2
        for i in range(1, len(df)):
            ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2
        return ha_open, ha_close

    # ---------- Z-SCORE STATUS ----------
    @staticmethod
    def get_zscore_status(df, lookback=20):
        if len(df) < lookback + 1: return 0, 0
        roll = df['close'].rolling(lookback)
        mean = roll.mean()
        std = roll.std()
        z_series = (df['close'] - mean) / std
        return z_series.iloc[-1], z_series.iloc[-2]

    # ---------- ORDER BLOCK ----------
    @staticmethod
    def detect_valid_ob(df, atr, direction):
        for i in range(-10, -3):
            body = abs(df['close'].iloc[i] - df['open'].iloc[i])
            impulse = abs(df['close'].iloc[i+1] - df['open'].iloc[i+1])
            if impulse < body * 1.8: continue
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

    # ---------- ZONE INTEGRITY ----------
    @staticmethod
    def check_zone_integrity(df, zone, lookback=5):
        if zone is None: return False
        zone_low = zone[0]
        recent_closes = df['close'].tail(lookback)
        for c in recent_closes:
            if c < zone_low: return False
        return True

    # ---------- GRADE INSTITUTIONNEL ----------
    @staticmethod
    def get_institutional_grade_v2(df_d, df_w, direction):
        if len(df_d) < 200: return "C"
        price_d = df_d['close'].iloc[-1]
        sma200_d = df_d['close'].rolling(200).mean().iloc[-1]
        ema50_d = df_d['close'].ewm(span=50).mean().iloc[-1]
        ema21_d = df_d['close'].ewm(span=21).mean().iloc[-1]
        
        if len(df_w) < 51: return "C"
        price_w = df_w['close'].iloc[-1]
        sma200_w = df_w['close'].rolling(50).mean().iloc[-1] 
        
        if direction == "BUY":
            cond_d = price_d > sma200_d and ema50_d > sma200_d and price_d > ema21_d
            cond_w = price_w > sma200_w
            if cond_d and cond_w: return "A+"
            if cond_d: return "A"
        else: 
            cond_d = price_d < sma200_d and ema50_d < sma200_d and price_d < ema21_d
            cond_w = price_w < sma200_w
            if cond_d and cond_w: return "A+"
            if cond_d: return "A"
        return "C"

    # ---------- UTILITAIRES ----------
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

# ==========================================
# LOGIQUE PRINCIPALE V6.2
# ==========================================
def calculate_signal_probability_v620(
    df_m5, df_m15, df_h1, df_d, df_w,
    symbol, direction, live_price, spread, cs_scores, strict_mode
):
    # 0. Initialisation
    price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
    atr = QuantEngine.calculate_atr(df_m5)
    params = get_asset_params(symbol)
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    midnight_open = QuantEngine.get_midnight_open_ny(df_m5)
    
    debug_info = {}
    
    # --- STRICT MODE CONFIG ---
    min_adx = 25 if strict_mode else 20
    min_z_abs = 2.0 if strict_mode else 1.5
    
    # -----------------------------------------------------------
    # 1. CONTEXTE H1 (AJOUT DI DIRECTIONNEL)
    # -----------------------------------------------------------
    adx_h1, adx_dir = QuantEngine.calculate_adx_and_di(df_h1)
    hma_h1 = QuantEngine.calculate_hma(df_h1['close'])
    hma_h1_green = hma_h1.iloc[-1] > hma_h1.iloc[-2]
    price_h1 = df_h1['close'].iloc[-1]
    
    if adx_h1 <= min_adx: 
        reason = f"H1 ADX {adx_h1:.1f} < {min_adx}" + (" (Strict)" if strict_mode else "")
        return 0, debug_info, 0, reason, {}
    
    # FIX V6.2 : Vérifier la direction ADX (+DM vs -DM)
    if direction == "BUY":
        if adx_dir != 1: return 0, debug_info, 0, "ADX Direction BAISSIÈRE", {} # +DM doit dominer
        if not hma_h1_green: return 0, debug_info, 0, "H1 HMA Rouge", {}
        if price_h1 <= hma_h1.iloc[-1]: return 0, debug_info, 0, "Prix sous HMA H1", {}
    else: # SELL
        if adx_dir != -1: return 0, debug_info, 0, "ADX Direction HAUSSIÈRE", {} # -DM doit dominer
        if hma_h1_green: return 0, debug_info, 0, "H1 HMA Verte (Sell)", {}
        if price_h1 >= hma_h1.iloc[-1]: return 0, debug_info, 0, "Prix sur HMA H1 (Sell)", {}

    debug_info['H1'] = f"✅ ADX:{adx_h1:.0f} DI:{'PLUS' if adx_dir==1 else 'MINUS'} HMA: {'UP' if hma_h1_green else 'DN'}"

    # -----------------------------------------------------------
    # 2. ALIGNEMENT M15 (FIX STRUCTURE)
    # -----------------------------------------------------------
    hma_m15 = QuantEngine.calculate_hma(df_m15['close'])
    hma_m15_green = hma_m15.iloc[-1] > hma_m15.iloc[-2]
    
    ha_o_m15, ha_c_m15 = QuantEngine.get_ha_ohlc(df_m15)
    ha_m15_green = ha_c_m15.iloc[-1] > ha_o_m15.iloc[-1]
    
    # FIX V6.2 : Structure plus robuste (2 points consécutifs)
    # Buy: Low[-1] > Low[-3] (HL sur 2 bougies) ET Low[-1] > Low[-5] (Tendance HL)
    if direction == "BUY":
        structure_ok = (df_m15['low'].iloc[-1] > df_m15['low'].iloc[-3]) and (df_m15['low'].iloc[-1] > df_m15['low'].iloc[-5])
    else:
        structure_ok = (df_m15['high'].iloc[-1] < df_m15['high'].iloc[-3]) and (df_m15['high'].iloc[-1] < df_m15['high'].iloc[-5])
    
    # Matrice des forces
    cs_aligned = False
    if "_" in symbol and cs_scores:
        base, quote = symbol.split('_')
        gap = cs_scores.get(base, 0) - cs_scores.get(quote, 0)
        if direction == "BUY" and gap > 0: cs_aligned = True
        elif direction == "SELL" and gap < 0: cs_aligned = True

    if direction == "BUY":
        if not hma_m15_green: return 0, debug_info, 0, "M15 HMA Rouge", {}
        if not ha_m15_green: return 0, debug_info, 0, "M15 HA Rouge", {}
        if not structure_ok: return 0, debug_info, 0, "M15 Structure Faible", {}
        if not cs_aligned: return 0, debug_info, 0, "CS Non Aligné", {}
    else:
        if hma_m15_green: return 0, debug_info, 0, "M15 HMA Verte (Sell)", {}
        if ha_m15_green: return 0, debug_info, 0, "M15 HA Verte (Sell)", {}
        if not structure_ok: return 0, debug_info, 0, "M15 Structure Faible", {}
        if not cs_aligned: return 0, debug_info, 0, "CS Non Aligné", {}

    debug_info['M15'] = f"✅ HMA/HA OK | Structure Robuste | CS OK"

    # -----------------------------------------------------------
    # 3. ZONE & CONTEXTE (M5) + TRACKING ZONE
    # -----------------------------------------------------------
    # Midnight Rule
    if midnight_open:
        if direction == "BUY" and price >= midnight_open:
            return 0, debug_info, 0, "Prix > Midnight (Buy)", {}
        if direction == "SELL" and price <= midnight_open:
            return 0, debug_info, 0, "Prix < Midnight (Sell)", {}
    
    # PDL Rule
    if pdl is not None and direction == "BUY" and price <= pdl:
        return 0, debug_info, 0, "Prix < PDL (Achat cassé)", {}
    
    # OB/FVG Detection
    ob_valid, ob_zone = QuantEngine.detect_valid_ob(df_m5, atr, direction)
    fvg_valid, fvg_zone = QuantEngine.detect_fvg(df_m5, atr, direction)
    
    target_zone = None
    zone_type = ""
    
    # FIX V6.2 : Priorité OB > FVG
    if ob_valid:
        target_zone = ob_zone
        zone_type = "OB"
    elif fvg_valid:
        target_zone = fvg_zone
        zone_type = "FVG"
    else:
        return 0, debug_info, 0, "Aucune Zone OB/FVG", {}
    
    if not QuantEngine.check_zone_integrity(df_m5, target_zone, lookback=5):
        return 0, debug_info, 0, f"{zone_type} Cassée", {}
        
    # FIX V6.2 : Vérifier si la zone est déjà utilisée (Session State)
    # Si le prix est dans une zone qu'on a déjà signalé, on refuse.
    if symbol in st.session_state.active_zones:
        existing = st.session_state.active_zones[symbol]
        # Nettoyage : si le prix est sorti de l'ancienne zone, on la supprime
        old_low, old_high = existing['zone']
        if price < old_low or price > old_high:
            del st.session_state.active_zones[symbol]
        else:
            # Le prix est DANS l'ancienne zone. Refus.
            return 0, debug_info, 0, "Zone Déjà Utilisée (Re-entry)", {}

    debug_info['ZONE'] = f"✅ {zone_type} Validée (Fresh)"

    # -----------------------------------------------------------
    # 4. DÉCLENCHEUR M5 & Z-SCORE
    # -----------------------------------------------------------
    hma_m5 = QuantEngine.calculate_hma(df_m5['close'])
    hma_m5_green = hma_m5.iloc[-1] > hma_m5.iloc[-2]
    
    ha_o_m5, ha_c_m5 = QuantEngine.get_ha_ohlc(df_m5)
    z_curr, z_prev = QuantEngine.get_zscore_status(df_m5, lookback=20)
    
    z_buy_ok = (z_curr < -min_z_abs) and (z_curr > z_prev)
    z_sell_ok = (z_curr > min_z_abs) and (z_curr < z_prev)

    if direction == "BUY":
        if not hma_m5_green: return 0, debug_info, 0, "M5 HMA Rouge", {}
        ha_prev_red = ha_c_m5.iloc[-2] < ha_o_m5.iloc[-2]
        ha_curr_green = ha_c_m5.iloc[-1] > ha_o_m5.iloc[-1]
        if not (ha_prev_red and ha_curr_green): return 0, debug_info, 0, "Pas de HA Flip Buy", {}
        if not z_buy_ok: return 0, debug_info, 0, f"Z-Score {z_curr:.2f} (Need < -{min_z_abs})", {}
    else: # SELL
        if hma_m5_green: return 0, debug_info, 0, "M5 HMA Verte", {}
        ha_prev_green = ha_c_m5.iloc[-2] > ha_o_m5.iloc[-2]
        ha_curr_red = ha_c_m5.iloc[-1] < ha_o_m5.iloc[-1]
        if not (ha_prev_green and ha_curr_red): return 0, debug_info, 0, "Pas de HA Flip Sell", {}
        if not z_sell_ok: return 0, debug_info, 0, f"Z-Score {z_curr:.2f} (Need > {min_z_abs})", {}

    debug_info['TRIGGER'] = f"Z:{z_curr:.2f}"

    # -----------------------------------------------------------
    # 5. STRICT MODE: GRADE INSTITUTIONNEL
    # -----------------------------------------------------------
    if strict_mode:
        grade = QuantEngine.get_institutional_grade_v2(df_d, df_w, direction)
        debug_info['GRADE'] = grade
        if grade != "A+":
            return 0, debug_info, 0, f"Grade Institutionnel: {grade} (Need A+)", {}

    # -----------------------------------------------------------
    # 6. SCORING FINAL & ZONE REGISTRATION
    # -----------------------------------------------------------
    score_base = 0.85 if strict_mode else 0.80
    if adx_h1 > 30: score_base += 0.05
    if cs_aligned: score_base += 0.10
    final_score = min(score_base, 1.0)

    sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
    tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
    
    # FIX V6.2 : Enregistrer la zone pour empêcher le re-entry
    st.session_state.active_zones[symbol] = {'zone': target_zone, 'dir': direction}

    details = {
        "adx_val": adx_h1,
        "z_score": z_curr,
        "hma_slope": 1 if direction=="BUY" else -1,
        "ha_status": "🟢" if direction=="BUY" else "🔴",
        "midnight": f"{midnight_open:.5f}" if midnight_open else "N/A",
        "pdh_pdl": f"{pdl:.5f}/{pdh:.5f}" if pdh else "N/A",
        "confluence": f"{zone_type} + CS" + (" + Grade A+" if strict_mode else ""),
        "session": QuantEngine.get_trading_session(datetime.now(pytz.utc)),
        "zone_status": f"{zone_type} ACTIVE",
        "target": "SL/TP Calculated",
        "debug": debug_info,
        "grade": debug_info.get('GRADE', '-')
    }
    
    return final_score, details, atr / price * 100, None, {}

# ==========================================
# SCANNER PRINCIPAL V6.2
# ==========================================
def run_scan_v620(api, min_prob, current_time_utc, strict_mode, filter_asian):
    cs_scores = get_currency_strength_rsi(api)
    signals = []
    rejected_log = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, sym in enumerate(ASSETS):
        progress_bar.progress((i+1)/len(ASSETS))
        mode_str = "STRICT" if strict_mode else "STANDARD"
        status_text.markdown(f"⏳ Scan [{mode_str}]: **{sym}** ({i+1}/{len(ASSETS)})")
        try:
            # FIX V6.2 : Filtre Session Asiatique (Optionnel)
            if filter_asian:
                session = QuantEngine.get_trading_session(current_time_utc)
                if session == "ASIAN":
                    # Exceptions: Gold/Indices sometimes move in Asian, but let's keep it strict as requested
                    if "XAU" not in sym and "US30" not in sym:
                        rejected_log.append(f"{sym}: Session Asiatique Filtrée")
                        continue
            
            df_h1 = api.get_candles(sym, "H1", 50)
            df_m15 = api.get_candles(sym, "M15", 100)
            df_m5 = api.get_candles(sym, "M5", 200)
            df_d = api.get_candles(sym, "D", 250)
            df_w = api.get_candles(sym, "W", 150) 
            
            live_price, spread_pips = api.get_realtime_price_and_spread(sym)
            
            if df_m5.empty or df_h1.empty or df_m15.empty or df_d.empty or df_w.empty: continue
            
            for direction in ["BUY", "SELL"]:
                prob, details, atr_pct, reject_reason, _ = calculate_signal_probability_v620(
                    df_m5, df_m15, df_h1, df_d, df_w, sym, direction, live_price, spread_pips, cs_scores, strict_mode
                )
                
                if reject_reason: 
                    rejected_log.append(f"{sym} {direction}: {reject_reason}")
                    continue
                
                if prob < min_prob: continue
                
                if check_dynamic_correlation_conflict({'symbol': sym, 'type': direction}, signals, cs_scores):
                    rejected_log.append(f"{sym} {direction}: Corrélation Conflit")
                    continue

                price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
                atr = QuantEngine.calculate_atr(df_m5)
                params = get_asset_params(sym)
                sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
                tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
                
                signals.append({
                    'symbol': sym, 'type': direction, 'price': price, 'prob': prob, 'score_display': prob * 10,
                    'details': details, 'atr_pct': atr_pct, 'sl': sl, 'tp': tp, 'rr': params['tp_rr'],
                    'spread': spread_pips, 'enhanced_metrics': {}, 'is_strict': strict_mode
                })

        except Exception as e: 
            rejected_log.append(f"❌ {sym} Err: {str(e)[:30]}")
            continue

    progress_bar.empty()
    status_text.empty()
    return sorted(signals, key=lambda x: x['prob'], reverse=True), rejected_log

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
# AFFICHAGE V6.2
# ==========================================
def display_sig_v620(s):
    is_buy = s['type'] == 'BUY'
    col_type = "#10b981" if is_buy else "#ef4444"
    bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if is_buy else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
    d = s['details']
    is_strict = s.get('is_strict', False)
    
    title_suffix = " [STRICT]" if is_strict else ""
    
    with st.expander(f"{'📈' if is_buy else '📉'} {s['symbol']}  |  {s['type']}  |  SCORE {s['score_display']:.1f}/10{title_suffix}", expanded=True):
        st.markdown(f"""
        <div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};margin-bottom:10px;">
            <span style="font-size:1.5em;font-weight:900;color:white;">{s['symbol']}</span>
            <span style="float:right;color:white;font-size:1.2em;">{s['price']:.5f}</span>
        </div>""", unsafe_allow_html=True)
        
        badges = [
            f"<span class='badge badge-blue'>H1 ADX: {d.get('adx_val',0):.0f}</span>",
            f"<span class='badge badge-blue'>M15 Aligned</span>",
            f"<span class='badge badge-gold'>{d.get('zone_status', 'ZONE')}</span>",
            f"<span class='badge'>Z-Score: {d.get('z_score',0):.2f}</span>",
            f"<span class='badge badge-session'>{d.get('session', 'NA')}</span>"
        ]
        if is_strict:
             badges.append(f"<span class='badge badge-strict'>INST. A+ GRADE</span>")
             
        st.markdown(f"<div style='text-align:center;margin-bottom:10px'>{' '.join(badges)}</div>", unsafe_allow_html=True)
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Confluence", d.get('confluence', '-'))
        c2.metric("Midnight", d.get('midnight', 'N/A'))
        c3.metric("PDH / PDL", d.get('pdh_pdl', 'N/A'))
        
        col_sl, col_tp = st.columns(2)
        col_sl.info(f"🛑 SL: {s['sl']:.5f}")
        col_tp.success(f"🎯 TP: {s['tp']:.5f}")
        
        with st.expander("🔍 Analyse Top-Down"):
            st.write(f"**H1 Context:** ADX > {25 if is_strict else 20}, DI Directionnel {'HAUSSIER' if is_buy else 'BAISSIER'}.")
            st.write(f"**M15 Alignment:** HMA/HA Green, Structure Robuste (2 pts), CS OK.")
            st.write(f"**M5 Trigger:** HA Flip + Z-Score {'< -2.0' if is_strict else '< -1.5'}.")
            if d.get('debug'):
                st.json(d['debug'])

# ==========================================
# MAIN
# ==========================================
def main():
    st.title("🛡️ BLUESTAR ULTIMATE V6.2")
    st.markdown("<p style='text-align:center;color:#94a3b8;'>Top-Down Logic (H1 → M15 → M5) + ADX Direction + Zone Tracking</p>", unsafe_allow_html=True)
    
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
        st.header("⚙️ Paramètres V6.2")
        strict_mode = st.checkbox("🔒 Mode STRICT (Institutional)", value=False, help="Filtre ADX > 25, Z-Score < 2.0 et Grade A+ requis")
        filter_asian = st.checkbox("🕶️ Filtrer Session Asiatique", value=True, help="Ignore les signaux pendant la session Asiatique (faible volatilité)")
        min_prob = st.slider("Score Min", 60, 95, 75, 5)
        
        if strict_mode:
            st.warning("⚠️ Mode Strict activé.")
        if filter_asian:
            st.info("🚫 Session Asiatique filtrée.")

    if st.button("🔍 SCANNER V6.2"):
        with st.spinner("Analyse Top-Down en cours..."):
            api = OandaClient()
            results, logs = run_scan_v620(api, min_prob/100, current_time_utc, strict_mode, filter_asian)
            
        if not results:
            st.warning("⚠️ Aucun signal validé.")
            with st.expander("Logs de rejet (Debug)"):
                for log in logs[:50]: st.text(log)
        else:
            st.success(f"✅ {len(results)} Signal(s) Validé(s)")
            for r in results: display_sig_v620(r)
            with st.expander("Logs de rejet"):
                for log in logs[:50]: st.text(log)

if __name__ == "__main__":
    main()
