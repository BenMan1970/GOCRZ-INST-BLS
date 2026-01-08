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
# CONFIGURATION & STYLE (THEME BLEU V5.0)
# ==========================================
warnings.simplefilter(action='ignore', category=FutureWarning)
st.set_page_config(page_title="Bluestar Ultimate V5.0", layout="centered", page_icon="🛡️")

LOG_FILE = "bluestar_v5_log.csv"

if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            "timestamp", "symbol", "direction", "price", "score", "hma_slope", "ha_status", 
            "midnight_pos", "pdl_prox", "pdh_prox", "sl", "tp"
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
    .timestamp-box { background: rgba(59, 130, 246, 0.1); border-left: 3px solid #3b82f6; padding: 8px 12px; border-radius: 6px; font-size: 0.85em; color: #93c5fd; margin: 10px 0; font-family: 'Courier New', monospace; }
    .inst-label { color:#94a3b8; font-size:0.8em; text-transform: uppercase; letter-spacing: 1px; }
    .inst-val { font-size: 1.1em; font-weight: 700; color: #f1f5f9; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# CLIENT API & SESSION STATE
# ==========================================
if 'cache' not in st.session_state: st.session_state.cache = {}
if 'signal_history' not in st.session_state: st.session_state.signal_history = {}
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
# MOTEUR D'INDICATEURS V2 (Sans RSI/Volume)
# ==========================================
class QuantEngine:
    @staticmethod
    def calculate_atr(df, period=14):
        if len(df) < period + 1: return 0
        h, l, c = df['high'], df['low'], df['close']
        tr = pd.concat([h-l, abs(h-c.shift(1)), abs(l-c.shift(1))], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean().iloc[-1]

    @staticmethod
    def calculate_hma(series, period=20):
        if len(series) < period: return pd.Series([])
        half = int(period / 2)
        sqrt_p = int(np.sqrt(period))
        wma_half = series.rolling(half).mean()
        wma_full = series.rolling(period).mean()
        diff = 2 * wma_half - wma_full
        hma = diff.rolling(sqrt_p).mean()
        return hma

    @staticmethod
    def get_hma_color_trend(hma_series):
        # Retourne 1 si haussier (croissant), -1 si baissier, 0 si plat
        if len(hma_series) < 5: return 0
        # On regarde la pente sur 3 bougies
        slope = hma_series.iloc[-1] - hma_series.iloc[-3]
        if slope > 0: return 1
        elif slope < 0: return -1
        return 0

    @staticmethod
    def calculate_ha_smoothed(df, period=3):
        # Calcul standard Heiken Ashi
        ha_close = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha_open = np.zeros(len(df))
        ha_open[0] = (df['open'].iloc[0] + df['close'].iloc[0]) / 2
        for i in range(1, len(df)):
            ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2
        ha_high = df['high'].combine(pd.Series(ha_open), max).combine(ha_close, max)
        ha_low = df['low'].combine(pd.Series(ha_open), min).combine(ha_close, min)
        
        # Lissage (EMA sur les données HA)
        smooth_open = pd.Series(ha_open).ewm(span=period).mean()
        smooth_close = ha_close.ewm(span=period).mean()
        
        # Détermination de la couleur de la dernière bougie lissée
        if smooth_close.iloc[-1] > smooth_open.iloc[-1]: return 1 # Bull
        else: return -1 # Bear

    @staticmethod
    def calculate_adx(df, period=14):
        if len(df) < period * 2: return 0
        high, low, close = df['high'], df['low'], df['close']
        plus_dm = high.diff()
        minus_dm = -low.diff()
        tr = pd.concat([high-low, abs(high-close.shift()), abs(low-close.shift())], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0.0)
        minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0.0)
        plus_di = 100 * pd.Series(plus_dm).ewm(span=period, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus_dm).ewm(span=period, adjust=False).mean() / atr
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.ewm(span=period, adjust=False).mean()
        return adx.iloc[-1]

    @staticmethod
    def detect_structure_zscore(df, lookback=20):
        if len(df) < lookback + 1: return 0
        window = df['close'].iloc[-lookback:]
        try:
            z_score = stats.zscore(window)[-1]
            return z_score
        except: return 0

    @staticmethod
    def get_context_levels(df_m5, df_d):
        """
        Retourne (PDH, PDL, Midnight_Open NY, Current_Price)
        """
        # 1. PDH / PDL depuis Daily
        # df_d contient D[-1] (hier) et D[0] (aujourd'hui en cours)
        # On veut hier (index -2 si quotidien, -1 si intraday et qu'on a la bougie d'hier fermée)
        # En général df_d[-1] est la bougie en cours si 'D', ou la dernière fermée si 'D' dans past.
        # Supposons que df_d inclut la bougie en cours. L'hier est -2.
        if len(df_d) < 2: return None, None, None, None
        
        pdh = df_d['high'].iloc[-2]
        pdl = df_d['low'].iloc[-2]
        
        # 2. Midnight Open NY
        try:
            ny_tz = pytz.timezone('America/New_York')
            df_ny = df_m5.copy()
            df_ny['time'] = pd.to_datetime(df_ny['time'], utc=True).dt.tz_convert(ny_tz)
            
            # On cherche la bougie à 00:00 NY de la session actuelle
            # On filtre par heure 0
            midnight_candles = df_ny[df_ny['time'].dt.hour == 0]
            if not midnight_candles.empty:
                # On prend le close de la dernière bougie de minuit trouvée (ou open, ici open)
                midnight_open = midnight_candles.iloc[-1]['open']
            else:
                # Fallback : Open du jour actuel en Daily
                midnight_open = df_d['open'].iloc[-1]
                
            current_price = df_m5['close'].iloc[-1]
            return pdh, pdl, midnight_open, current_price
        except Exception as e:
            logging.error(f"Erreur Context Levels: {e}")
            return None, None, None, None

    @staticmethod
    def check_session_killzone(current_dt_utc, force_open=False):
        if force_open: return "24/7_FORCED"
        hour = current_dt_utc.hour
        if 7 <= hour < 13: return "LDN_SESSION"
        if 13 <= hour < 16: return "NY_OVERLAP"
        if 16 <= hour < 22: return "NY_SESSION"
        return None

# ==========================================
# CURRENCY STRENGTH (Inchangé car efficace)
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
# FILTRE CORRÉLATION ÉTENDU
# ==========================================
def check_dynamic_correlation_conflict(new_signal, existing_signals, cs_scores):
    if not existing_signals: return False
    new_sym = new_signal['symbol']
    new_type = new_signal['type']
    if "_" not in new_sym: return False
    base, quote = new_sym.split('_')
    
    # Matrice étendue V5.0
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
            if corr > 0.85 and new_type != ex_type: return True # Forte corr, sens opposé = conflit
            if corr < -0.85 and new_type == ex_type: return True # Forte negative, meme sens = conflit
    return False

# ==========================================
# LOGIQUE DE PROBABILITÉ V5.0
# ==========================================
def calculate_signal_probability_v5(df_m5, df_h4, df_d, df_w, symbol, current_time_utc, spread_pips, force_open=False):
    # 1. Setup de base
    atr = QuantEngine.calculate_atr(df_m5)
    atr_pct = (atr / df_m5['close'].iloc[-1]) * 100
    params = get_asset_params(symbol)
    
    if atr_pct < params['atr_threshold'] * 0.3:
        return 0, {}, atr_pct, "Low Volatility (ATR)"
    
    session = QuantEngine.check_session_killzone(current_time_utc, force_open)
    if session is None and not force_open:
        return 0, {}, atr_pct, "Off Session"
    
    # 2. Calcul des indicateurs
    hma_series = QuantEngine.calculate_hma(df_m5['close'], 20)
    hma_trend = QuantEngine.get_hma_color_trend(hma_series)
    
    ha_trend = QuantEngine.calculate_ha_smoothed(df_m5)
    
    adx_val = QuantEngine.calculate_adx(df_h4)
    z_score = QuantEngine.detect_structure_zscore(df_h4)
    
    pdh, pdl, midnight_open, curr_price = QuantEngine.get_context_levels(df_m5, df_d)
    
    # 3. Détermination de la direction potentielle (Trigger HMA + HA)
    # On ne trade que si HMA et HA sont d'accord
    direction_candidate = None
    if hma_trend == 1 and ha_trend == 1:
        direction_candidate = "BUY"
    elif hma_trend == -1 and ha_trend == -1:
        direction_candidate = "SELL"
    
    if direction_candidate is None:
        return 0, {}, atr_pct, "Indicateurs non alignés"
        
    # 4. Filtrage par Zones (PDH/PDL + Midnight)
    # Logique demandée : Achat proche PDL et sous Midnight. Vente proche PDH et au dessus Midnight.
    daily_range = pdh - pdl
    
    if direction_candidate == "BUY":
        # Proximité PDL : Le prix doit être dans le tiers bas de la journée précédente
        dist_to_pdl = (curr_price - pdl) / daily_range if daily_range > 0 else 1
        
        # Check Midnight
        under_midnight = curr_price < midnight_open
        
        if dist_to_pdl > 0.4: return 0, {}, atr_pct, "Trop loin PDL"
        if not under_midnight: return 0, {}, atr_pct, "Dessus Midnight (No Buy)"
        if z_score > 2.0: return 0, {}, atr_pct, "Z-Score Trop Haut"
        
    else: # SELL
        # Proximité PDH : Prix doit être dans le tiers haut
        dist_to_pdh = (pdh - curr_price) / daily_range if daily_range > 0 else 1
        
        # Check Midnight
        above_midnight = curr_price > midnight_open
        
        if dist_to_pdh > 0.4: return 0, {}, atr_pct, "Trop loin PDH"
        if not above_midnight: return 0, {}, atr_pct, "Dessous Midnight (No Sell)"
        if z_score < -2.0: return 0, {}, atr_pct, "Z-Score Trop Bas"

    # 5. Scoring (0 à 1)
    prob_factors = []
    weights = []
    details = {}
    
    # ADX Score
    adx_score = min((adx_val - 15) / 15, 1.0) if adx_val > 15 else 0
    prob_factors.append(adx_score)
    weights.append(0.2)
    details['adx_val'] = adx_val
    
    # Session Bonus
    session_score = 1.0 if session in ["LDN_SESSION", "NY_OVERLAP"] else 0.6
    prob_factors.append(session_score)
    weights.append(0.1)
    
    # Structure Score (Inverse du Z-Score pour la sécurité)
    # Plus on est proche de 0, mieux c'est pour l'entrée
    structure_score = max(0, 1 - (abs(z_score)/2.0))
    prob_factors.append(structure_score)
    weights.append(0.2)
    details['structure_z'] = z_score
    
    # HMA/HA Alignment (Ici c'est 1 car on a passé le filtre, mais on peut pondérer la pente)
    slope_strength = abs(hma_series.iloc[-1] - hma_series.iloc[-5])
    trend_score = min(slope_strength / (atr * 2), 1.0) # Pente relative à l'ATR
    prob_factors.append(trend_score)
    weights.append(0.3)
    details['hma_slope'] = hma_trend
    
    # Zone Proximity Score (Plus c'est proche, mieux c'est)
    if direction_candidate == "BUY":
        prox_score = 1.0 - ((curr_price - pdl)/daily_range) if daily_range > 0 else 0.5
    else:
        prox_score = 1.0 - ((pdh - curr_price)/daily_range) if daily_range > 0 else 0.5
    prob_factors.append(prox_score)
    weights.append(0.2)
    
    total_weight = sum(weights)
    weighted_prob = sum(p * w for p, w in zip(prob_factors, weights)) / total_weight
    
    # Ajout des details pour l'affichage
    details['ha_status'] = ha_trend
    details['midnight_pos'] = "Under" if direction_candidate == "BUY" else "Above"
    
    return max(0, min(1.0, weighted_prob)), details, atr_pct, None, direction_candidate

# ==========================================
# SCANNER
# ==========================================
def run_scan_v50_blue(api, min_prob, strict_mode, current_time_utc, force_open=False):
    cs_scores = get_currency_strength_rsi(api)
    signals = []
    rejected_log = []
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, sym in enumerate(ASSETS):
        progress_bar.progress((i+1)/len(ASSETS))
        status_text.markdown(f"⏳ Analyse: **{sym}** ({i+1}/{len(ASSETS)})")
        
        try:
            # Fetch Data
            df_d_raw = api.get_candles(sym, "D", 250) # Besoin de D pour PDH/PDL
            time.sleep(0.02)
            df_m5 = api.get_candles(sym, "M5", 200) # M5 pour précision entry et HA
            time.sleep(0.02)
            df_h4 = api.get_candles(sym, "H4", 100)
            
            if df_m5.empty or df_h4.empty or df_d_raw.empty: 
                rejected_log.append(f"{sym}: Données vides")
                continue
            
            # Process Daily
            df_d = df_d_raw.iloc[-100:].copy()
            df_w = df_d_raw.set_index('time').resample('W-FRI').agg({
                'open':'first', 'high':'max', 'low':'min', 'close':'last'
            }).dropna().reset_index()
            
            _, spread_pips = api.get_realtime_spread(sym)
            
            # Calcul Probabilité V5
            # Note: La fonction determine elle-même la direction interne
            prob, details, atr_pct, reject_reason, direction = calculate_signal_probability_v5(
                df_m5, df_h4, df_d, df_w, sym, current_time_utc, spread_pips, force_open
            )
            
            if reject_reason:
                rejected_log.append(f"{sym}: {reject_reason}")
                continue
                
            if prob < min_prob: 
                rejected_log.append(f"{sym}: Score Faible ({prob:.2f})")
                continue
            
            # Strict Mode sur structure ou spread ?
            if strict_mode and spread_pips > 3.0: 
                rejected_log.append(f"{sym}: Strict Spread")
                continue

            # Corrélation
            temp_signal_obj = {'symbol': sym, 'type': direction}
            if check_dynamic_correlation_conflict(temp_signal_obj, signals, cs_scores): 
                rejected_log.append(f"{sym}: Corrélation")
                continue
            
            # Currency Strength Alignment
            cs_aligned = False
            if "_" in sym:
                base, quote = sym.split('_')
                if cs_scores and base in cs_scores and quote in cs_scores:
                    gap = cs_scores.get(base, 0) - cs_scores.get(quote, 0)
                    if direction == "BUY" and gap > 0: cs_aligned = True
                    elif direction == "SELL" and gap < 0: cs_aligned = True
            elif "XAU" in sym or "US30" in sym: cs_aligned = True 

            # Calcul SL/TP
            price = df_m5['close'].iloc[-1]
            atr = QuantEngine.calculate_atr(df_m5)
            params = get_asset_params(sym)
            
            sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
            tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
            
            signals.append({
                'symbol': sym,
                'type': direction,
                'price': price,
                'prob': prob,
                'score_display': prob * 10,
                'details': details,
                'atr_pct': atr_pct,
                'detection_time': datetime.now(),
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
# AFFICHAGE V5.0
# ==========================================
def display_sig_v5(s):
    is_buy = s['type'] == 'BUY'
    col_type = "#10b981" if is_buy else "#ef4444"
    bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if is_buy else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
    
    sc = s['score_display']
    
    # Label basé sur le score et le contexte
    label = "TACTICAL"
    if sc >= 8.0: label = "INSTITUTIONAL 💎"
    elif sc >= 7.0: label = "STRATEGIC ⭐"

    with st.expander(f"{s['symbol']}  |  {s['type']}  |  {label}  [{sc:.1f}/10]", expanded=True):
        st.markdown(f"""
        <div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};
                    display:flex;justify-content:space-between;align-items:center;">
            <div>
                <span style="font-size:1.8em;font-weight:900;color:white;">{s['symbol']}</span>
                <span style="background:rgba(255,255,255,0.2);padding:2px 8px;border-radius:4px;color:white;margin-left:10px;">{s['type']}</span>
            </div>
            <div style="text-align:right;">
                <div style="font-size:1.4em;font-weight:bold;color:white;">{s['price']:.5f}</div>
                <div style="font-size:0.75em;color:#cbd5e1;">SPR: {s['spread']:.1f} | ATR: {s['atr_pct']:.3f}%</div>
            </div>
        </div>""", unsafe_allow_html=True)
        
        badges = []
        # Badge HMA Status
        hma_txt = "📈 HMA BULL" if s['details']['hma_slope'] > 0 else "📉 HMA BEAR"
        badges.append(f"<span class='badge badge-blue'>{hma_txt}</span>")
        
        # Badge HA Status
        ha_txt = "🟢 HA BULL" if s['details']['ha_status'] > 0 else "🔴 HA BEAR"
        badges.append(f"<span class='badge badge-blue'>{ha_txt}</span>")
        
        if s['cs_aligned']: badges.append("<span class='badge badge-gold'>CS ALIGNÉ</span>")
        
        st.markdown(f"<div style='margin-top:10px;text-align:center'>{' '.join(badges)}</div>", unsafe_allow_html=True)
        
        st.markdown("---")
        
        # Bloc Context
        c1, c2, c3, c4 = st.columns(4)
        
        with c1:
            st.markdown("<div class='inst-label'>ADX TREND</div>", unsafe_allow_html=True)
            st.markdown(f"<div class='inst-val'>{s['details']['adx_val']:.1f}</div>", unsafe_allow_html=True)
            
        with c2:
            st.markdown("<div class='inst-label'>MIDNIGHT POS</div>", unsafe_allow_html=True)
            st.markdown(f"<div class='inst-val'>{s['details']['midnight_pos']}</div>", unsafe_allow_html=True)

        with c3:
            st.markdown("<div class='inst-label'>STRUCTURE Z</div>", unsafe_allow_html=True)
            z = s['details']['structure_z']
            col_z = "#f87171" if abs(z) > 1.5 else "#60a5fa"
            st.markdown(f"<div class='inst-val' style='color:{col_z}'>{z:.2f}</div>", unsafe_allow_html=True)

        with c4:
            st.markdown("<div class='inst-label'>RISK / REWARD</div>", unsafe_allow_html=True)
            st.markdown(f"<div class='inst-val' style='color:#f59e0b'>1 : {s['rr']}</div>", unsafe_allow_html=True)
            
        st.write("")
        ex1, ex2 = st.columns(2)
        ex1.info(f"🛑 **SL:** {s['sl']:.5f}")
        ex2.success(f"🎯 **TP:** {s['tp']:.5f}")

# ==========================================
# MAIN
# ==========================================
def main():
    st.title("🛡️ BLUESTAR ULTIMATE V5.0")
    st.markdown("<p style='text-align:center;color:#94a3b8;'>HMA/HA Trend + PDH/PDL/Midnight Logic</p>", unsafe_allow_html=True)
    
    st.sidebar.markdown(f"🕒 **Heure UTC Scanner:** {datetime.now(pytz.utc).strftime('%H:%M')}")

    with st.sidebar:
        st.header("⚙️ Configuration V5")
        col_st, col_fo = st.columns(2)
        strict_mode = col_st.checkbox("🔥 Strict", value=False)
        force_open = col_fo.checkbox("🔓 24/7", value=False)
        min_prob_display = st.slider("Confiance Min (%)", 50, 95, 75, 5)
        
    if st.button("🔍 Lancer le Scan V5", type="primary"):
        api = OandaClient()
        current_sim_time = datetime.now(pytz.utc)
        
        with st.spinner("Analyse V5 (HMA/HA + Context Zones)..."):
            results, logs = run_scan_v50_blue(api, min_prob_display/100.0, strict_mode, current_sim_time, force_open)
        
        if not results:
            st.warning("Aucun signal haute probabilité détecté.")
            if logs:
                with st.expander("Logs de diagnostic"):
                    st.write(logs)
        else:
            st.success(f"{len(results)} Opportunités détectées")
            for sig in results:
                display_sig_v5(sig)

if __name__ == "__main__":
    main()
