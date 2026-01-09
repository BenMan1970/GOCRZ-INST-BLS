import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
import logging
import os
from datetime import datetime
import pytz
import warnings

warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

st.set_page_config(page_title="Bluestar V6.6", layout="centered", page_icon="🛡️")

if 'trade_logs' not in st.session_state:
    st.session_state.trade_logs = []
if 'active_zones' not in st.session_state:
    st.session_state.active_zones = {}

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700;900&display=swap');
    * { font-family: 'Roboto', sans-serif; }
    .stApp { background-color: #0f1117; background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%); }
    .main .block-container { max-width: 950px; padding-top: 2rem; }
    h1 {
        background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        font-weight: 900; font-size: 2.2em; text-align: center; margin-bottom: 0.5em;
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
    .badge-elite { background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); color: black; font-size: 1em; }
    .badge-premium { background: linear-gradient(135deg, #C0C0C0 0%, #808080 100%); color: black; font-size: 1em; }
</style>
""", unsafe_allow_html=True)

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
            st.error(f"⚠️ Configuration API: {e}")
            st.stop()

    def get_candles(self, instrument, granularity, count):
        key = f"{instrument}_{granularity}_{count}"
        if key in st.session_state.cache:
            ts, data = st.session_state.cache[key]
            timeout = {"M5": 15, "M15": 60, "H1": 300, "H4": 300, "D": 900, "W": 3600}.get(granularity, 900)
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
        except:
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
        except: return 0, 0

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
        return {'type': 'INDEX', 'sl_base': 2.0, 'tp_rr': 3.0}
    if any(met in symbol for met in ["XAU", "XPT", "XAG"]):
        return {'type': 'COMMODITY', 'sl_base': 1.8, 'tp_rr': 2.5}
    return {'type': 'FOREX', 'sl_base': 1.5, 'tp_rr': 2.0}

class QuantEngine:
    @staticmethod
    def calculate_atr(df, period=14):
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low'] - df['close'].shift()).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(span=period).mean().iloc[-1]

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
        direction = 1 if plus_di.iloc[-1] > minus_di.iloc[-1] else -1
        return adx, direction

    @staticmethod
    def calculate_hma(series, period=20):
        half = int(period / 2)
        sqrt = int(np.sqrt(period))
        wma_half = series.rolling(half).apply(lambda x: np.dot(x, np.arange(1, half+1)) / np.arange(1, half+1).sum(), raw=True)
        wma_full = series.rolling(period).apply(lambda x: np.dot(x, np.arange(1, period+1)) / np.arange(1, period+1).sum(), raw=True)
        diff = 2 * wma_half - wma_full
        return diff.rolling(sqrt).apply(lambda x: np.dot(x, np.arange(1, sqrt+1)) / np.arange(1, sqrt+1).sum(), raw=True)

    @staticmethod
    def get_ha_ohlc(df):
        ha_close = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha_open = ha_close.copy()
        ha_open.iloc[0] = (df['open'].iloc[0] + df['close'].iloc[0]) / 2
        for i in range(1, len(df)):
            ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2
        return ha_open, ha_close

    @staticmethod
    def get_zscore_status(df, lookback=20):
        if len(df) < lookback + 1: return 0, 0
        roll = df['close'].rolling(lookback)
        mean = roll.mean()
        std = roll.std()
        z_series = (df['close'] - mean) / std
        return z_series.iloc[-1], z_series.iloc[-2]

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

    @staticmethod
    def get_trading_session(current_time_utc):
        hour = current_time_utc.hour
        if 23 <= hour or hour < 8: return "ASIAN"
        elif 8 <= hour < 16: return "LONDON"
        elif 13 <= hour < 21: return "NY"
        else: return "OFF"

    @staticmethod
    def get_midnight_open_ny(df):
        try:
            ny_tz = pytz.timezone('America/New_York')
            df_ny = df.copy()
            df_ny['time'] = pd.to_datetime(df_ny['time'], utc=True).dt.tz_convert(ny_tz)
            midnight_candle = df_ny[df_ny['time'].dt.hour == 0]
            if not midnight_candle.empty: return midnight_candle.iloc[-1]['open']
            return None
        except: return None

    @staticmethod
    def get_pdh_pdl(df_d):
        if len(df_d) < 2: return None, None
        return df_d['high'].iloc[-2], df_d['low'].iloc[-2]

    @staticmethod
    def detect_valid_ob(df, atr, direction):
        if len(df) < 30: return False, None
        price = df['close'].iloc[-1]
        proximity_max = atr * 0.5
        
        for i in range(-50, -5):
            if abs(i) > len(df): continue
            
            candle = df.iloc[i]
            next_candle = df.iloc[i+1]
            
            body = abs(candle['close'] - candle['open'])
            if body < atr * 0.15: continue
            
            impulse = abs(next_candle['close'] - next_candle['open'])
            if impulse < body * 1.3: continue
            
            zone_low = candle['low']
            zone_high = candle['high']
            
            if direction == "BUY":
                if candle['close'] >= candle['open']: continue
                if next_candle['close'] <= next_candle['open']: continue
                
                distance_to_zone = price - zone_high
                if -proximity_max <= distance_to_zone <= proximity_max:
                    zone_broken = False
                    for j in range(i+2, 0):
                        if df['low'].iloc[j] < zone_low:
                            zone_broken = True
                            break
                    if not zone_broken:
                        return True, (zone_low, zone_high)
            else:
                if candle['close'] <= candle['open']: continue
                if next_candle['close'] >= next_candle['open']: continue
                
                distance_to_zone = zone_low - price
                if -proximity_max <= distance_to_zone <= proximity_max:
                    zone_broken = False
                    for j in range(i+2, 0):
                        if df['high'].iloc[j] > zone_high:
                            zone_broken = True
                            break
                    if not zone_broken:
                        return True, (zone_low, zone_high)
        
        return False, None

    @staticmethod
    def detect_fvg(df, atr, direction):
        if len(df) < 20: return False, None
        price = df['close'].iloc[-1]
        proximity_max = atr * 0.3
        
        for i in range(-40, -3):
            if abs(i) > len(df) - 2: continue
            
            c1 = df.iloc[i-2]
            c2 = df.iloc[i-1]
            c3 = df.iloc[i]
            
            if direction == "BUY":
                gap = c3['low'] - c1['high']
                if gap < atr * 0.20: continue
                
                c2_bullish = c2['close'] > c2['open']
                c2_body = abs(c2['close'] - c2['open'])
                if not c2_bullish or c2_body < atr * 0.3: continue
                
                zone_low = c1['high']
                zone_high = c3['low']
                
                distance_to_zone = price - zone_high
                if -proximity_max <= distance_to_zone <= proximity_max:
                    return True, (zone_low, zone_high)
            else:
                gap = c1['low'] - c3['high']
                if gap < atr * 0.20: continue
                
                c2_bearish = c2['close'] < c2['open']
                c2_body = abs(c2['close'] - c2['open'])
                if not c2_bearish or c2_body < atr * 0.3: continue
                
                zone_low = c3['high']
                zone_high = c1['low']
                
                distance_to_zone = zone_low - price
                if -proximity_max <= distance_to_zone <= proximity_max:
                    return True, (zone_low, zone_high)
        
        return False, None

def calculate_signal_v66(df_m5, df_m15, df_h1, df_d, df_w, symbol, direction, live_price, spread, cs_scores, min_adx, use_zones, strict_grade):
    """Logique stricte selon le document"""
    
    price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
    atr = QuantEngine.calculate_atr(df_m5)
    midnight_open = QuantEngine.get_midnight_open_ny(df_m5)
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    
    score = 0
    reasons = []
    
    # ========== FILTRE H1 (OBLIGATOIRE) ==========
    # 1. ADX H1 > 20
    adx_h1, adx_dir = QuantEngine.calculate_adx_and_di(df_h1)
    if adx_h1 < min_adx:
        return 0, {}, 0, f"ADX {adx_h1:.1f} < {min_adx}", {}
    
    # 2. Direction ADX alignée
    if direction == "BUY" and adx_dir != 1:
        return 0, {}, 0, "ADX Direction Baissière", {}
    if direction == "SELL" and adx_dir != -1:
        return 0, {}, 0, "ADX Direction Haussière", {}
    
    score += 15
    reasons.append(f"✅ ADX {adx_h1:.1f}")
    
    # 3. HMA H1 verte + prix au-dessus
    hma_h1 = QuantEngine.calculate_hma(df_h1['close'])
    if len(hma_h1) < 3:
        return 0, {}, 0, "HMA H1 données insuffisantes", {}
    
    hma_h1_green = hma_h1.iloc[-2] > hma_h1.iloc[-3]
    price_h1 = df_h1['close'].iloc[-1]
    
    if direction == "BUY":
        if not hma_h1_green:
            return 0, {}, 0, "HMA H1 Rouge", {}
        if price_h1 <= hma_h1.iloc[-2]:
            return 0, {}, 0, "Prix sous HMA H1", {}
    else:
        if hma_h1_green:
            return 0, {}, 0, "HMA H1 Verte", {}
        if price_h1 >= hma_h1.iloc[-2]:
            return 0, {}, 0, "Prix sur HMA H1", {}
    
    score += 15
    reasons.append("✅ HMA H1 + Prix OK")
    
    # ========== FILTRE M15 (OBLIGATOIRE) ==========
    # 4. HMA M15 alignée
    hma_m15 = QuantEngine.calculate_hma(df_m15['close'])
    hma_m15_green = hma_m15.iloc[-2] > hma_m15.iloc[-3]
    
    if direction == "BUY":
        if not hma_m15_green:
            return 0, {}, 0, "HMA M15 Rouge", {}
    else:
        if hma_m15_green:
            return 0, {}, 0, "HMA M15 Verte", {}
    
    # 5. Heiken Ashi M15 (bougie clôturée)
    ha_o_m15, ha_c_m15 = QuantEngine.get_ha_ohlc(df_m15)
    ha_m15_green = ha_c_m15.iloc[-2] > ha_o_m15.iloc[-2]
    
    if direction == "BUY":
        if not ha_m15_green:
            return 0, {}, 0, "HA M15 Rouge", {}
    else:
        if ha_m15_green:
            return 0, {}, 0, "HA M15 Verte", {}
    
    # 6. Structure M15
    if direction == "BUY":
        structure_ok = (df_m15['low'].iloc[-2] > df_m15['low'].iloc[-4])
    else:
        structure_ok = (df_m15['high'].iloc[-2] < df_m15['high'].iloc[-4])
    
    if not structure_ok:
        return 0, {}, 0, "Structure M15 faible", {}
    
    # 7. Matrice des forces
    cs_aligned = False
    if "_" in symbol and cs_scores:
        base, quote = symbol.split('_')
        gap = cs_scores.get(base, 0) - cs_scores.get(quote, 0)
        if direction == "BUY" and gap > 0:
            cs_aligned = True
        elif direction == "SELL" and gap < 0:
            cs_aligned = True
    
    if not cs_aligned:
        return 0, {}, 0, "CS Non Aligné", {}
    
    score += 15
    reasons.append("✅ M15 Aligné + CS OK")
    
    # ========== ZONE D'INTÉRÊT (INDISPENSABLE) ==========
    zone_text = "NO_ZONE"
    
    if use_zones:
        # 8. Détection zone
        ob_valid, ob_zone = QuantEngine.detect_valid_ob(df_m5, atr, direction)
        fvg_valid, fvg_zone = QuantEngine.detect_fvg(df_m5, atr, direction)
        
        if ob_valid:
            score += 10
            reasons.append("✅ ORDER BLOCK")
            zone_text = "OB"
        elif fvg_valid:
            score += 8
            reasons.append("✅ FVG")
            zone_text = "FVG"
        elif pdl and direction == "BUY" and abs(price - pdl) < atr * 0.5:
            score += 6
            reasons.append("✅ PDL")
            zone_text = "PDL"
        elif pdh and direction == "SELL" and abs(price - pdh) < atr * 0.5:
            score += 6
            reasons.append("✅ PDH")
            zone_text = "PDH"
        else:
            return 0, {}, 0, "Aucune Zone", {}
    else:
        score += 10
        reasons.append("ℹ️ Zones désactivées")
    
    # 9. Prix sous/sur Midnight
    if midnight_open:
        if direction == "BUY" and price >= midnight_open:
            return 0, {}, 0, "Prix > Midnight", {}
        if direction == "SELL" and price <= midnight_open:
            return 0, {}, 0, "Prix < Midnight", {}
        score += 10
        reasons.append("✅ Midnight OK")
    else:
        score += 5
        reasons.append("⚠️ Midnight N/A")
    
    # 10. PDL/PDH respectés
    if pdl and direction == "BUY" and price <= pdl:
        return 0, {}, 0, "Prix < PDL", {}
    if pdh and direction == "SELL" and price >= pdh:
        return 0, {}, 0, "Prix > PDH", {}
    
    # ========== DÉCLENCHEUR M5 (TIMING) ==========
    # 11. HMA M5
    hma_m5 = QuantEngine.calculate_hma(df_m5['close'])
    hma_m5_green = hma_m5.iloc[-2] > hma_m5.iloc[-3]
    
    if direction == "BUY":
        if not hma_m5_green:
            return 0, {}, 0, "HMA M5 Rouge", {}
    else:
        if hma_m5_green:
            return 0, {}, 0, "HMA M5 Verte", {}
    
    # 12. HA Flip M5 (ANTI-REPAINT)
    ha_o_m5, ha_c_m5 = QuantEngine.get_ha_ohlc(df_m5)
    
    if direction == "BUY":
        ha_prev_red = ha_c_m5.iloc[-3] < ha_o_m5.iloc[-3]
        ha_curr_green = ha_c_m5.iloc[-2] > ha_o_m5.iloc[-2]
        ha_flip = ha_prev_red and ha_curr_green
    else:
        ha_prev_green = ha_c_m5.iloc[-3] > ha_o_m5.iloc[-3]
        ha_curr_red = ha_c_m5.iloc[-2] < ha_o_m5.iloc[-2]
        ha_flip = ha_prev_green and ha_curr_red
    
    if not ha_flip:
        return 0, {}, 0, "Pas de HA Flip", {}
    
    score += 20
    reasons.append("✅ HA Flip Confirmé")
    
    # 13. Z-Score (timing)
    z_curr, z_prev = QuantEngine.get_zscore_status(df_m5, lookback=20)
    
    if direction == "BUY":
        if z_curr < -1.5 and z_curr > z_prev:
            score += 10
            reasons.append(f"✅ Z-Score {z_curr:.2f}")
        else:
            score += 3
            reasons.append(f"⚠️ Z-Score {z_curr:.2f}")
    else:
        if z_curr > 1.5 and z_curr < z_prev:
            score += 10
            reasons.append(f"✅ Z-Score {z_curr:.2f}")
        else:
            score += 3
            reasons.append(f"⚠️ Z-Score {z_curr:.2f}")
    
    # ========== GRADE INSTITUTIONNEL ==========
    grade = QuantEngine.get_institutional_grade_v2(df_d, df_w, direction)
    
    if strict_grade:
        if grade != "A+":
            return 0, {}, 0, f"Grade {grade} (Need A+)", {}
        score += 10
        reasons.append("✅ Grade A+")
    else:
        if grade == "A+":
            score += 10
            reasons.append("✅ Grade A+")
        elif grade == "A":
            score += 7
            reasons.append("✅ Grade A")
        else:
            score += 3
            reasons.append(f"⚠️ Grade {grade}")
    
    # ========== VALIDATION FINALE ==========
    if score < 60:
        return 0, {}, 0, f"Score {score}/100 insuffisant", {}
    
    # Classification
    if score >= 85:
        quality = "ELITE 🏆"
    elif score >= 75:
        quality = "PREMIUM ⭐"
    else:
        quality = "STANDARD ✅"
    
    # SL/TP
    params = get_asset_params(symbol)
    sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
    tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
    
    details = {
        "quality": quality,
        "score": score,
        "reasons": reasons,
        "midnight": f"{midnight_open:.5f}" if midnight_open else "N/A",
        "pdh_pdl": f"{pdh:.5f} / {pdl:.5f}" if pdh else "N/A",
        "zone_type": zone_text,
        "adx": adx_h1,
        "z_score": z_curr,
        "grade": grade,
        "session": QuantEngine.get_trading_session(datetime.now(pytz.utc)),
    }
    
    return score / 100, details, atr / price * 100, None, {}


def run_scan_v66(api, min_score, current_time_utc, filter_asian, min_adx, use_zones, strict_grade):
    cs_scores = get_currency_strength_rsi(api)
    signals = []
    rejected_log = []
    
    stats = {
        'total': len(ASSETS),
        'adx_rejected': 0,
        'hma_rejected': 0,
        'ha_rejected': 0,
        'zone_rejected': 0,
        'cs_rejected': 0,
        'midnight_rejected': 0,
        'score_rejected': 0,
        'session_filtered': 0,
        'other': 0
    }
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, sym in enumerate(ASSETS):
        progress_bar.progress((i+1)/len(ASSETS))
        status_text.markdown(f"⏳ **{sym}** ({i+1}/{len(ASSETS)})")
        
        try:
            # Session filter
            if filter_asian:
                session = QuantEngine.get_trading_session(current_time_utc)
                if session == "ASIAN" and "XAU" not in sym and "US30" not in sym:
                    stats['session_filtered'] += 1
                    rejected_log.append(f"{sym}: Session Asiatique")
                    continue
            
            # Lazy load H1
            df_h1 = api.get_candles(sym, "H1", 50)
            if df_h1.empty: continue
            
            # Pre-check ADX
            adx_h1, adx_dir = QuantEngine.calculate_adx_and_di(df_h1)
            if adx_h1 < min_adx:
                stats['adx_rejected'] += 1
                rejected_log.append(f"{sym}: ADX {adx_h1:.1f}")
                continue
            
            # Full load
            df_m15 = api.get_candles(sym, "M15", 100)
            df_m5 = api.get_candles(sym, "M5", 200)
            df_d = api.get_candles(sym, "D", 250)
            df_w = api.get_candles(sym, "W", 150)
            
            live_price, spread_pips = api.get_realtime_price_and_spread(sym)
            
            if df_m5.empty or df_m15.empty or df_d.empty or df_w.empty:
                continue
            
            # Test directions
            for direction in ["BUY", "SELL"]:
                if direction == "BUY" and adx_dir != 1: continue
                if direction == "SELL" and adx_dir != -1: continue
                
                prob, details, atr_pct, reject_reason, _ = calculate_signal_v66(
                    df_m5, df_m15, df_h1, df_d, df_w, sym, direction,
                    live_price, spread_pips, cs_scores, min_adx, use_zones, strict_grade
                )
                
                if reject_reason:
                    if "ADX" in reject_reason: stats['adx_rejected'] += 1
                    elif "HMA" in reject_reason: stats['hma_rejected'] += 1
                    elif "HA" in reject_reason or "Flip" in reject_reason: stats['ha_rejected'] += 1
                    elif "Zone" in reject_reason or "OB" in reject_reason or "FVG" in reject_reason: stats['zone_rejected'] += 1
                    elif "CS" in reject_reason: stats['cs_rejected'] += 1
                    elif "Midnight" in reject_reason: stats['midnight_rejected'] += 1
                    elif "Score" in reject_reason: stats['score_rejected'] += 1
                    else: stats['other'] += 1
                    
                    rejected_log.append(f"{sym} {direction}: {reject_reason}")
                    continue
                
                score_100 = prob * 100
                if score_100 < min_score:
                    stats['score_rejected'] += 1
                    rejected_log.append(f"{sym} {direction}: Score {score_100:.0f}")
                    continue
                
                price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
                atr = QuantEngine.calculate_atr(df_m5)
                params = get_asset_params(sym)
                sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
                tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
                
                signals.append({
                    'symbol': sym,
                    'type': direction,
                    'price': price,
                    'prob': prob,
                    'score_display': score_100,
                    'details': details,
                    'atr_pct': atr_pct,
                    'sl': sl,
                    'tp': tp,
                    'rr': params['tp_rr'],
                    'spread': spread_pips
                })
        
        except Exception as e:
            stats['other'] += 1
            rejected_log.append(f"❌ {sym}: {str(e)[:40]}")
            continue
    
    progress_bar.empty()
    status_text.empty()
    return sorted(signals, key=lambda x: x['prob'], reverse=True), rejected_log, stats


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
        except: continue
    
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
        total_score = 0.0
        count = 0
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
                count += 1
        
        if count > 0:
            final_scores[curr] = total_score / count
        else:
            final_scores[curr] = 5.0
    
    st.session_state.cs_data = {'data': final_scores, 'time': now}
    return final_scores


def display_sig_v66(s):
    is_buy = s['type'] == 'BUY'
    col_type = "#10b981" if is_buy else "#ef4444"
    bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if is_buy else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
    d = s['details']
    
    quality_badge = ""
    if "ELITE" in d['quality']:
        quality_badge = "<span class='badge-elite'>🏆 ELITE</span>"
    elif "PREMIUM" in d['quality']:
        quality_badge = "<span class='badge-premium'>⭐ PREMIUM</span>"
    else:
        quality_badge = "<span class='badge badge-blue'>✅ STANDARD</span>"
    
    with st.expander(f"{'📈' if is_buy else '📉'} {s['symbol']} | {s['type']} | {s['score_display']:.0f}/100", expanded=True):
        st.markdown(f"""
        <div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};margin-bottom:10px;">
            <span style="font-size:1.5em;font-weight:900;color:white;">{s['symbol']}</span>
            <span style="float:right;color:white;font-size:1.2em;">{s['price']:.5f}</span><br>
            <div style='margin-top:10px;'>{quality_badge}</div>
        </div>""", unsafe_allow_html=True)
        
        st.info(f"**Score:** {d['score']}/100 | **Zone:** {d['zone_type']} | **Session:** {d['session']} | **Grade:** {d['grade']}")
        
        st.markdown("### 📋 Critères Validés")
        for reason in d['reasons']:
            if "✅" in reason:
                st.success(reason)
            elif "⚠️" in reason:
                st.warning(reason)
            elif "ℹ️" in reason:
                st.info(reason)
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Midnight", d.get('midnight', 'N/A'))
        c2.metric("PDH / PDL", d.get('pdh_pdl', 'N/A'))
        c3.metric("ADX", f"{d.get('adx', 0):.1f}")
        
        col_sl, col_tp = st.columns(2)
        col_sl.info(f"🛑 SL: {s['sl']:.5f}")
        col_tp.success(f"🎯 TP: {s['tp']:.5f} (RR: {s['rr']:.1f})")


def main():
    st.title("🛡️ BLUESTAR V6.6")
    
    current_time_utc = datetime.now(pytz.utc)
    session = QuantEngine.get_trading_session(current_time_utc)
    session_colors = {"ASIAN": "#f59e0b", "LONDON": "#10b981", "NY": "#3b82f6", "OFF": "#6b7280"}
    
    st.sidebar.markdown(f"""
        <div style='background:{session_colors.get(session, "#6b7280")};padding:10px;border-radius:8px;text-align:center;margin-bottom:15px;'>
            <div style='font-size:0.8em;color:white;opacity:0.8;'>🕒 {current_time_utc.strftime('%H:%M')} UTC</div>
            <div style='font-size:1.1em;font-weight:700;color:white;'>📍 {session}</div>
        </div>
    """, unsafe_allow_html=True)
    
    with st.sidebar:
        st.header("⚙️ Paramètres")
        
        min_score = st.slider("Score Minimum", 60, 95, 70, 5)
        
        st.markdown("---")
        adx_enabled = st.checkbox("✅ Filtrer ADX", value=True)
        min_adx = st.slider("ADX Min", 15, 30, 20, 1) if adx_enabled else 0
        
        use_zones = st.checkbox("🎯 Zones (OB/FVG)", value=True)
        strict_grade = st.checkbox("📊 Grade Strict (A+)", value=False)
        filter_asian = st.checkbox("🕶️ Filtrer Asiatique", value=True)
    
    if st.button("🔍 SCANNER"):
        with st.spinner("Analyse..."):
            api = OandaClient()
            results, logs, stats = run_scan_v66(
                api, min_score, current_time_utc, filter_asian,
                min_adx, use_zones, strict_grade
            )
        
        if not results:
            st.warning("⚠️ Aucun signal")
            
            st.subheader("📊 Rejets")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Session", stats['session_filtered'])
            col2.metric("ADX", stats['adx_rejected'])
            col3.metric("HMA", stats['hma_rejected'])
            col4.metric("HA", stats['ha_rejected'])
            
            col5, col6, col7, col8 = st.columns(4)
            col5.metric("Zone", stats['zone_rejected'])
            col6.metric("CS", stats['cs_rejected'])
            col7.metric("Midnight", stats['midnight_rejected'])
            col8.metric("Score", stats['score_rejected'])
            
            with st.expander("📜 Logs (50 premiers)"):
                for log in logs[:50]:
                    st.text(log)
        else:
            st.success(f"✅ {len(results)} Signal(s)")
            
            elite = sum(1 for r in results if "ELITE" in r['details']['quality'])
            premium = sum(1 for r in results if "PREMIUM" in r['details']['quality'])
            standard = len(results) - elite - premium
            
            col1, col2, col3 = st.columns(3)
            col1.metric("🏆 Elite", elite)
            col2.metric("⭐ Premium", premium)
            col3.metric("✅ Standard", standard)
            
            for r in results:
                display_sig_v66(r)
            
            with st.expander("📜 Logs"):
                for log in logs[:50]:
                    st.text(log)

if __name__ == "__main__":
    main()
