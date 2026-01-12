import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
import logging
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz
import warnings

warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

st.set_page_config(page_title="BlueStar Sniper Ultimate 7", layout="centered", page_icon="⭐")

if 'active_zones' not in st.session_state:
    st.session_state.active_zones = {}

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700;900&display=swap');
    * { font-family: 'Roboto', sans-serif; }
    .stApp { 
        background-color: #0a0e27; 
        background-image: 
            radial-gradient(at 50% 0%, rgba(59, 130, 246, 0.15) 0%, transparent 50%),
            radial-gradient(at 0% 100%, rgba(37, 99, 235, 0.1) 0%, transparent 50%);
    }
    .main .block-container { max-width: 1000px; padding-top: 2rem; }
    
    h1 {
        background: linear-gradient(135deg, #60a5fa 0%, #3b82f6 50%, #2563eb 100%);
        -webkit-background-clip: text; 
        -webkit-text-fill-color: transparent;
        font-weight: 900; 
        font-size: 2.5em; 
        text-align: left;
        margin-bottom: 0.3em;
        text-shadow: 0 0 30px rgba(59, 130, 246, 0.3);
        display: inline-block;
        vertical-align: middle;
        margin-left: 0px;
        margin-top: 0;
        margin-bottom: 0;
    }
    
    .star-logo {
        font-size: 2.8em;
        display: inline-block;
        vertical-align: middle;
        color: #60a5fa;
        filter: drop-shadow(0 0 15px rgba(96, 165, 250, 0.8));
        animation: pulse-star 2s infinite;
        margin-right: 10px;
    }
    
    @keyframes pulse-star {
        0%, 100% { 
            filter: drop-shadow(0 0 15px rgba(96, 165, 250, 0.8));
            transform: scale(1);
        }
        50% { 
            filter: drop-shadow(0 0 25px rgba(96, 165, 250, 1));
            transform: scale(1.05);
        }
    }
    
    .header-container {
        text-align: center;
        margin-bottom: 1em;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    
    .stButton>button {
        width: 100%; 
        border-radius: 12px; 
        height: 3.5em; 
        font-weight: 700; 
        font-size: 1.1em;
        border: 2px solid rgba(239, 68, 68, 0.5);
        background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
        color: white; 
        transition: all 0.3s ease;
        box-shadow: 0 4px 15px rgba(37, 99, 235, 0.4), 0 0 20px rgba(239, 68, 68, 0.2);
    }
    
    .stButton>button:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(59, 130, 246, 0.6), 0 0 30px rgba(239, 68, 68, 0.4);
        border-color: rgba(239, 68, 68, 0.7);
    }
    
    .streamlit-expanderHeader { 
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%) !important; 
        border: 2px solid rgba(59, 130, 246, 0.2); 
        border-radius: 12px; 
        color: #f1f5f9 !important; 
        padding: 1rem;
        transition: all 0.3s ease;
    }
    
    .streamlit-expanderHeader:hover {
        border-color: rgba(96, 165, 250, 0.5);
        box-shadow: 0 0 20px rgba(59, 130, 246, 0.2);
    }
    
    .streamlit-expanderContent { 
        background: linear-gradient(180deg, #0f172a 0%, #020617 100%); 
        border: 2px solid rgba(59, 130, 246, 0.15); 
        border-top: none; 
        border-bottom-left-radius: 12px; 
        border-bottom-right-radius: 12px; 
        padding: 20px;
    }
    
    .badge { 
        color: white; 
        padding: 6px 14px; 
        border-radius: 8px; 
        font-size: 0.8em; 
        font-weight: 700; 
        margin: 3px; 
        display: inline-block;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    }
    
    .badge-elite { 
        background: linear-gradient(135deg, #fbbf24 0%, #f59e0b 50%, #d97706 100%); 
        color: #1e293b;
        animation: pulse-gold 2s infinite;
    }
    
    .badge-premium { 
        background: linear-gradient(135deg, #60a5fa 0%, #3b82f6 100%); 
        animation: pulse-blue 2s infinite;
    }
    
    .badge-pirm {
        background: linear-gradient(135deg, #8b5cf6 0%, #6366f1 100%);
    }
    
    @keyframes pulse-gold {
        0%, 100% { box-shadow: 0 0 10px rgba(251, 191, 36, 0.4); }
        50% { box-shadow: 0 0 20px rgba(251, 191, 36, 0.8); }
    }
    
    @keyframes pulse-blue {
        0%, 100% { box-shadow: 0 0 10px rgba(59, 130, 246, 0.4); }
        50% { box-shadow: 0 0 20px rgba(59, 130, 246, 0.8); }
    }
    
    .metric-card {
        background: linear-gradient(135deg, rgba(30, 41, 59, 0.8) 0%, rgba(15, 23, 42, 0.8) 100%);
        border: 1px solid rgba(59, 130, 246, 0.2);
        border-radius: 10px;
        padding: 12px;
        margin: 5px 0;
    }
</style>
""", unsafe_allow_html=True)

@st.cache_data(ttl=300, show_spinner=False)
def fetch_candles_cached(instrument, granularity, count, token, env):
    try:
        client = oandapyV20.API(access_token=token, environment=env)
        params = {"count": count, "granularity": granularity, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)
        client.request(r)
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
    except:
        return pd.DataFrame()

class OandaClient:
    def __init__(self):
        try:
            self.access_token = st.secrets["OANDA_ACCESS_TOKEN"]
            self.account_id = st.secrets["OANDA_ACCOUNT_ID"]
            self.environment = st.secrets.get("OANDA_ENVIRONMENT", "practice")
            self.client = oandapyV20.API(access_token=self.access_token, environment=self.environment)
        except Exception as e:
            st.error(f"⚠️ API Configuration Error: {e}")
            st.stop()

    def get_candles(self, instrument, granularity, count):
        return fetch_candles_cached(instrument, granularity, count, self.access_token, self.environment)

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
        except: 
            return 0, 0

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

class VolumeProfileEngine:
    """
    Institutional-Grade Volume Profile Analysis
    Utilisé par les hedge funds pour identifier les zones de liquidité
    """
    
    @staticmethod
    def calculate_volume_profile(df, lookback=100, bins=50):
        """
        Calcule le Volume Profile avec POC, VAH, VAL
        
        Args:
            df: DataFrame avec colonnes ['high', 'low', 'close', 'volume']
            lookback: Nombre de bougies à analyser
            bins: Nombre de niveaux de prix (plus = précision)
        
        Returns:
            dict avec POC, VAH, VAL, profile_data, hvn_zones, lvn_zones
        """
        if len(df) < lookback:
            return None
        
        # Prendre les dernières bougies
        df_slice = df.iloc[-lookback:].copy()
        
        # Définir le range de prix
        price_min = df_slice['low'].min()
        price_max = df_slice['high'].max()
        price_range = price_max - price_min
        
        if price_range == 0:
            return None
        
        # Créer les bins de prix
        bin_size = price_range / bins
        price_levels = np.linspace(price_min, price_max, bins + 1)
        
        # Accumuler le volume à chaque niveau de prix
        volume_at_price = np.zeros(bins)
        
        for idx, row in df_slice.iterrows():
            candle_low = row['low']
            candle_high = row['high']
            candle_volume = row['volume']
            
            # Distribuer le volume proportionnellement sur le range de la bougie
            for i in range(bins):
                level_low = price_levels[i]
                level_high = price_levels[i + 1]
                
                # Calculer l'intersection entre la bougie et le niveau
                overlap_low = max(candle_low, level_low)
                overlap_high = min(candle_high, level_high)
                
                if overlap_high > overlap_low:
                    # Proportion du volume pour ce niveau
                    overlap_ratio = (overlap_high - overlap_low) / (candle_high - candle_low) if candle_high > candle_low else 1.0
                    volume_at_price[i] += candle_volume * overlap_ratio
        
        # POC (Point of Control) = niveau avec le plus de volume
        poc_index = np.argmax(volume_at_price)
        poc_price = (price_levels[poc_index] + price_levels[poc_index + 1]) / 2
        
        # Value Area (70% du volume total)
        total_volume = volume_at_price.sum()
        target_volume = total_volume * 0.70
        
        # Trouver VAH et VAL (expansion depuis POC)
        accumulated_volume = volume_at_price[poc_index]
        upper_index = poc_index
        lower_index = poc_index
        
        while accumulated_volume < target_volume and (upper_index < bins - 1 or lower_index > 0):
            # Étendre vers le haut ou le bas selon où il y a plus de volume
            upper_vol = volume_at_price[upper_index + 1] if upper_index < bins - 1 else 0
            lower_vol = volume_at_price[lower_index - 1] if lower_index > 0 else 0
            
            if upper_vol > lower_vol and upper_index < bins - 1:
                upper_index += 1
                accumulated_volume += upper_vol
            elif lower_index > 0:
                lower_index -= 1
                accumulated_volume += lower_vol
            else:
                break
        
        vah_price = price_levels[upper_index + 1]
        val_price = price_levels[lower_index]
        
        # High Volume Nodes (HVN) - Zones à fort volume (top 20%)
        volume_threshold_hvn = np.percentile(volume_at_price, 80)
        hvn_zones = []
        
        for i in range(bins):
            if volume_at_price[i] >= volume_threshold_hvn:
                hvn_zones.append({
                    'price_low': price_levels[i],
                    'price_high': price_levels[i + 1],
                    'volume': volume_at_price[i],
                    'strength': volume_at_price[i] / volume_at_price[poc_index]
                })
        
        # VWAP (Volume Weighted Average Price)
        vwap = (df_slice['close'] * df_slice['volume']).sum() / df_slice['volume'].sum()
        
        # Volume Delta (estimation acheteurs vs vendeurs)
        bullish_volume = df_slice[df_slice['close'] > df_slice['open']]['volume'].sum()
        bearish_volume = df_slice[df_slice['close'] < df_slice['open']]['volume'].sum()
        volume_delta = bullish_volume - bearish_volume
        volume_delta_ratio = volume_delta / total_volume if total_volume > 0 else 0
        
        return {
            'poc': poc_price,
            'vah': vah_price,
            'val': val_price,
            'vwap': vwap,
            'hvn_zones': hvn_zones,
            'volume_delta_ratio': volume_delta_ratio,
            'total_volume': total_volume,
        }
    
    @staticmethod
    def validate_zone_with_volume(zone_price_low, zone_price_high, vp_data, min_strength=0.6):
        """
        Valide si une zone (OB/FVG) coïncide avec un HVN
        """
        if not vp_data:
            return False, 0
        
        zone_mid = (zone_price_low + zone_price_high) / 2
        
        for hvn in vp_data['hvn_zones']:
            if hvn['price_low'] <= zone_mid <= hvn['price_high']:
                if hvn['strength'] >= min_strength:
                    return True, hvn['strength']
        
        return False, 0
    
    @staticmethod
    def get_institutional_levels(vp_data, current_price):
        """
        Identifie les niveaux clés pour les institutionnels
        """
        if not vp_data:
            return {}
        
        levels = {}
        
        # Position par rapport à la Value Area
        if vp_data['val'] <= current_price <= vp_data['vah']:
            levels['position'] = 'INSIDE_VALUE_AREA'
            levels['bias'] = 'NEUTRAL'
        elif current_price > vp_data['vah']:
            levels['position'] = 'ABOVE_VALUE_AREA'
            levels['bias'] = 'PREMIUM_ZONE'
        else:
            levels['position'] = 'BELOW_VALUE_AREA'
            levels['bias'] = 'DISCOUNT_ZONE'
        
        # Pression volume
        if vp_data['volume_delta_ratio'] > 0.1:
            levels['volume_pressure'] = 'BULLISH'
        elif vp_data['volume_delta_ratio'] < -0.1:
            levels['volume_pressure'] = 'BEARISH'
        else:
            levels['volume_pressure'] = 'NEUTRAL'
        
        return levels

class QuantEngine:
    @staticmethod
    def calculate_atr_wilder(df, period=14):
        """ATR Wilder (RMA)"""
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low'] - df['close'].shift()).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    @staticmethod
    def adx_wilder(df, di_length=14, adx_length=14):
        """ADX Wilder EXACT"""
        high = df['high']
        low = df['low']
        close = df['close']
        
        # 1. Directional Movement
        up = high.diff()
        down = -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        
        # 2. True Range
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        
        # 3. RMA (Wilder's Moving Average)
        def rma(series, length):
            return series.ewm(alpha=1/length, adjust=False).mean()
        
        atr = rma(tr, di_length)
        plus_di = 100 * rma(pd.Series(plus_dm), di_length) / atr
        minus_di = 100 * rma(pd.Series(minus_dm), di_length) / atr
        
        # 4. ADX
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        adx = rma(dx, adx_length)
        
        return adx.iloc[-1], plus_di.iloc[-1], minus_di.iloc[-1]

    @staticmethod
    def calculate_hma(series, period=20):
        """Hull Moving Average"""
        half = int(period / 2)
        sqrt = int(np.sqrt(period))
        wma_half = series.rolling(half).apply(lambda x: np.dot(x, np.arange(1, half+1)) / np.arange(1, half+1).sum(), raw=True)
        wma_full = series.rolling(period).apply(lambda x: np.dot(x, np.arange(1, period+1)) / np.arange(1, period+1).sum(), raw=True)
        diff = 2 * wma_half - wma_full
        return diff.rolling(sqrt).apply(lambda x: np.dot(x, np.arange(1, sqrt+1)) / np.arange(1, sqrt+1).sum(), raw=True)

    @staticmethod
    def calculate_rsi_ohlc4(df, period=10):
        """RSI 10 OHLC4 pour M5"""
        ohlc4 = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        delta = ohlc4.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        rs = gain.ewm(alpha=1/period, adjust=False).mean() / loss.ewm(alpha=1/period, adjust=False).mean()
        return 100 - (100 / (1 + rs))

    @staticmethod
    def calculate_rsi_standard(series, period=14):
        """RSI 14 Close pour H1"""
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        rs = gain.ewm(alpha=1/period, adjust=False).mean() / loss.ewm(alpha=1/period, adjust=False).mean()
        return 100 - (100 / (1 + rs))

    @staticmethod
    def get_ha_ohlc(df):
        """Heiken Ashi"""
        ha_close = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha_open = ha_close.copy()
        ha_open.iloc[0] = (df['open'].iloc[0] + df['close'].iloc[0]) / 2
        for i in range(1, len(df)):
            ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2
        return ha_open, ha_close

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
    def detect_structure(df, direction, lookback=20):
        """Détection HH/HL (BUY) ou LL/LH (SELL)"""
        if len(df) < lookback + 5: 
            return False
        
        if direction == "BUY":
            # Higher Highs / Higher Lows
            recent_high = df['high'].iloc[-5:].max()
            prev_high = df['high'].iloc[-lookback:-5].max()
            recent_low = df['low'].iloc[-5:].min()
            prev_low = df['low'].iloc[-lookback:-5].min()
            return recent_high > prev_high and recent_low > prev_low
        else:
            # Lower Lows / Lower Highs
            recent_low = df['low'].iloc[-5:].min()
            prev_low = df['low'].iloc[-lookback:-5].min()
            recent_high = df['high'].iloc[-5:].max()
            prev_high = df['high'].iloc[-lookback:-5].max()
            return recent_low < prev_low and recent_high < prev_high

    @staticmethod
    def detect_structure_zscore(df, lookback=20):
        """Détection de la déviation standard (Z-Score) pour le range"""
        if len(df) < lookback: return 0.0, "Pas assez de données"
        
        # Calcul simple du Z-Score sur le Close
        series = df['close']
        mean = series.rolling(lookback).mean().iloc[-1]
        std = series.rolling(lookback).std().iloc[-1]
        
        if std == 0: return 0.0, "Volatilité nulle"
        
        z_score = (series.iloc[-1] - mean) / std
        
        status = "Neutre"
        if z_score > 2.0: status = "Extension Haussière (Possible Reversal)"
        elif z_score < -2.0: status = "Extension Baissière (Possible Reversal)"
        elif -1.0 <= z_score <= 1.0: status = "Consolidation / Range"
        else: status = "Tendance Saine"
            
        return z_score, status

    @staticmethod
    def detect_valid_ob(df, atr, direction):
        """Order Block Detection"""
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
        """Fair Value Gap Detection"""
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

def calculate_signal_bluestar_v7(df_m5, df_m15, df_h1, df_d, df_w, symbol, direction, live_price, cs_scores, config):
    """
    BlueStar Sniper Pro V7.0 - FUSION ULTIME
    Combine: Logique stricte + PIRM Trigger + Zones institutionnelles
    """
    
    price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
    atr = QuantEngine.calculate_atr_wilder(df_m5)
    midnight_open = QuantEngine.get_midnight_open_ny(df_m5)
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    
    score = 0
    reasons = []
    trigger_type = "STANDARD"
    
    # ========================================
    # PHASE 1: BIAIS H1 (PRÉREQUIS OBLIGATOIRES)
    # ========================================
    
    # 1. ADX Wilder > seuil
    adx_h1, plus_di_h1, minus_di_h1 = QuantEngine.adx_wilder(df_h1, 14, 14)
    
    if adx_h1 < config['min_adx']:
        return 0, {}, 0, f"ADX {adx_h1:.1f} < {config['min_adx']}", {}
    
    score += 15
    reasons.append(f"✅ ADX Wilder {adx_h1:.1f}")
    
    # 2. Structure HH/HL ou LL/LH
    if not QuantEngine.detect_structure(df_h1, direction, lookback=20):
        return 0, {}, 0, "Structure H1 invalide", {}
    
    score += 10
    reasons.append("✅ Structure H1 (HH/HL)" if direction == "BUY" else "✅ Structure H1 (LL/LH)")
    
    # 3. HMA 50 H1 + Position prix
    hma50_h1 = QuantEngine.calculate_hma(df_h1['close'], 50)
    if len(hma50_h1) < 3:
        return 0, {}, 0, "HMA H1 données insuffisantes", {}
    
    price_h1 = df_h1['close'].iloc[-1]
    
    if direction == "BUY":
        if price_h1 <= hma50_h1.iloc[-1]:
            return 0, {}, 0, "Prix sous HMA 50 (H1)", {}
    else:
        if price_h1 >= hma50_h1.iloc[-1]:
            return 0, {}, 0, "Prix sur HMA 50 (H1)", {}
    
    score += 10
    reasons.append("✅ HMA 50 + Prix OK")
    
    # 4. RSI 14 H1 > 50 (BUY) ou < 50 (SELL)
    rsi_h1 = QuantEngine.calculate_rsi_standard(df_h1['close'], 14)
    if len(rsi_h1) < 2:
        return 0, {}, 0, "RSI H1 insuffisant", {}
    
    rsi_h1_val = rsi_h1.iloc[-1]
    
    if direction == "BUY" and rsi_h1_val <= 50:
        return 0, {}, 0, f"RSI H1 {rsi_h1_val:.1f} <= 50", {}
    if direction == "SELL" and rsi_h1_val >= 50:
        return 0, {}, 0, f"RSI H1 {rsi_h1_val:.1f} >= 50", {}
    
    score += 10
    reasons.append(f"✅ RSI H1 {rsi_h1_val:.1f}")
    
    # ========================================
    # PHASE 2: ALIGNEMENT M15
    # ========================================
    
    # 5. HMA 20 M15 alignée
    hma20_m15 = QuantEngine.calculate_hma(df_m15['close'], 20)
    if len(hma20_m15) < 3:
        return 0, {}, 0, "HMA M15 insuffisant", {}
    
    hma_m15_slope_up = hma20_m15.iloc[-1] > hma20_m15.iloc[-2]
    
    if direction == "BUY":
        if not hma_m15_slope_up:
            return 0, {}, 0, "HMA M15 Rouge", {}
    else:
        if hma_m15_slope_up:
            return 0, {}, 0, "HMA M15 Verte", {}
    
    # 6. Heiken Ashi M15
    ha_o_m15, ha_c_m15 = QuantEngine.get_ha_ohlc(df_m15)
    ha_m15_bullish = ha_c_m15.iloc[-2] > ha_o_m15.iloc[-2]
    
    if direction == "BUY":
        if not ha_m15_bullish:
            return 0, {}, 0, "HA M15 Rouge", {}
    else:
        if ha_m15_bullish:
            return 0, {}, 0, "HA M15 Verte", {}
    
    score += 10
    reasons.append("✅ M15 Aligné (HMA+HA)")
    
    # ========================================
    # PHASE 2.5: Z-SCORE STRUCTURE CHECK
    # ========================================
    
    # Check structure sur H1 pour éviter les ranges serrés
    z_score_val, z_status = QuantEngine.detect_structure_zscore(df_h1, lookback=20)
    
    if abs(z_score_val) < 0.5:
        return 0, {}, 0, f"Range serré (Z-Score {z_score_val:.2f})", {}
    
    score += 5
    reasons.append(f"✅ {z_status}")
    
    # ========================================
    # PHASE 3: CURRENCY STRENGTH
    # ========================================
    cs_valid = False
    if "_" in symbol and cs_scores:
        base, quote = symbol.split('_')
        b_force = cs_scores.get(base, {'force': 5.0})['force']
        q_force = cs_scores.get(quote, {'force': 5.0})['force']
        gap = b_force - q_force
        
        if direction == "BUY" and gap > 0.5:
            cs_valid = True
        elif direction == "SELL" and gap < -0.5:
            cs_valid = True
        
        if not cs_valid:
            return 0, {}, 0, f"CS Gap {gap:.1f} insuffisant", {}
        
        score += 5
        reasons.append(f"✅ CS Gap {gap:.1f}")
    
    # ========================================
    # PHASE 3: TRIGGER M5 (PIRM INSTITUTIONAL)
    # ========================================
    
    # 8. HMA 20 M5
    hma20_m5 = QuantEngine.calculate_hma(df_m5['close'], 20)
    if len(hma20_m5) < 5:
        return 0, {}, 0, "HMA M5 insuffisant", {}
    
    hma_m5_current = hma20_m5.iloc[-1]
    
    # 9. RSI 10 OHLC4 (La clé du PIRM)
    rsi_m5 = QuantEngine.calculate_rsi_ohlc4(df_m5, 10)
    if len(rsi_m5) < 5:
        return 0, {}, 0, "RSI M5 insuffisant", {}
    
    rsi_m5_current = rsi_m5.iloc[-1]
    rsi_m5_prev = rsi_m5.iloc[-4:-1]  # Les 3 bougies précédentes
    
    # 10. ADX M5 > 18 (Momentum check)
    adx_m5, _, _ = QuantEngine.adx_wilder(df_m5, 14, 14)
    if adx_m5 < 18:
        return 0, {}, 0, f"ADX M5 {adx_m5:.1f} < 18", {}
    
    # 11. PIRM TRIGGER VALIDATION
    pirm_valid = False
    
    if direction == "BUY":
        # Pullback vers HMA 20
        touched_hma = (df_m5['low'].iloc[-4:] <= hma_m5_current * 1.0005).any()
        if not touched_hma:
            return 0, {}, 0, "Pas de Pullback HMA", {}
        
        # RSI: Zone reload 45-50, puis break 50
        rsi_reload = ((rsi_m5_prev >= 45) & (rsi_m5_prev <= 50)).any()
        rsi_above_40 = (rsi_m5_prev >= 40).all()  # Ne doit PAS passer sous 40
        
        if not rsi_above_40:
            return 0, {}, 0, f"RSI cassé 40 ({rsi_m5_prev.min():.1f})", {}
        
        if rsi_reload and rsi_m5_current > 50:
            # Heiken Ashi vert
            ha_o_m5, ha_c_m5 = QuantEngine.get_ha_ohlc(df_m5)
            ha_bullish = ha_c_m5.iloc[-1] > ha_o_m5.iloc[-1]
            
            if ha_bullish:
                pirm_valid = True
                trigger_type = "PIRM"
            else:
                return 0, {}, 0, "HA M5 pas vert", {}
        else:
            if not rsi_reload:
                return 0, {}, 0, "Pas de Reload RSI (45-50)", {}
            else:
                return 0, {}, 0, f"RSI pas break 50 ({rsi_m5_current:.1f})", {}
    
    else:  # SELL
        # Pullback vers HMA 20
        touched_hma = (df_m5['high'].iloc[-4:] >= hma_m5_current * 0.9995).any()
        if not touched_hma:
            return 0, {}, 0, "Pas de Pullback HMA", {}
        
        # RSI: Zone reload 50-55, puis break 50
        rsi_reload = ((rsi_m5_prev >= 50) & (rsi_m5_prev <= 55)).any()
        rsi_below_60 = (rsi_m5_prev <= 60).all()  # Ne doit PAS dépasser 60
        
        if not rsi_below_60:
            return 0, {}, 0, f"RSI cassé 60 ({rsi_m5_prev.max():.1f})", {}
        
        if rsi_reload and rsi_m5_current < 50:
            # Heiken Ashi rouge
            ha_o_m5, ha_c_m5 = QuantEngine.get_ha_ohlc(df_m5)
            ha_bearish = ha_c_m5.iloc[-1] < ha_o_m5.iloc[-1]
            
            if ha_bearish:
                pirm_valid = True
                trigger_type = "PIRM"
            else:
                return 0, {}, 0, "HA M5 pas rouge", {}
        else:
            if not rsi_reload:
                return 0, {}, 0, "Pas de Reload RSI (50-55)", {}
            else:
                return 0, {}, 0, f"RSI pas break 50 ({rsi_m5_current:.1f})", {}
    
    score += 25
    reasons.append(f"🔥 PIRM TRIGGER: RSI {rsi_m5_current:.1f}")
    
    # ========================================
    # PHASE 4: ZONES & CONFLUENCES (BONUS)
    # ========================================
    
    zone_text = "NO_ZONE"
    ob_zone = None
    fvg_zone = None
    
    if config['use_zones']:
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
            reasons.append("✅ PDL Zone")
            zone_text = "PDL"
        elif pdh and direction == "SELL" and abs(price - pdh) < atr * 0.5:
            score += 6
            reasons.append("✅ PDH Zone")
            zone_text = "PDH"
    else:
        score += 5
    
    # Midnight validation
    if midnight_open:
        if direction == "BUY" and price < midnight_open:
            score += 5
            reasons.append("✅ Discount Zone")
        elif direction == "SELL" and price > midnight_open:
            score += 5
            reasons.append("✅ Premium Zone")
    
    # ========================================
    # PHASE 4.5: VOLUME PROFILE CONFLUENCE (BONUS)
    # ========================================
    
    vp_score = 0
    vp_info = "NO_VP"
    vp_details = {}
    
    if config.get('use_vp', True):
        vp_data = VolumeProfileEngine.calculate_volume_profile(df_h1, lookback=100, bins=50)
        
        if vp_data:
            levels = VolumeProfileEngine.get_institutional_levels(vp_data, price)
            
            # BONUS 1: Prix près du POC (Point of Control)
            if abs(price - vp_data['poc']) < atr * 0.3:
                vp_score += 10
                vp_info = "POC"
                reasons.append("⭐ VP: Prix @ POC")
            
            # BONUS 2: Order Block dans HVN (High Volume Node)
            if ob_zone:
                is_valid, strength = VolumeProfileEngine.validate_zone_with_volume(
                    ob_zone[0], ob_zone[1], vp_data, min_strength=0.6
                )
                if is_valid:
                    bonus = int(strength * 10)
                    vp_score += bonus
                    vp_info = "OB+HVN"
                    reasons.append(f"⭐ VP: OB dans HVN ({strength:.1f}x)")
            
            # BONUS 3: FVG validé par volume
            if fvg_zone:
                is_valid, strength = VolumeProfileEngine.validate_zone_with_volume(
                    fvg_zone[0], fvg_zone[1], vp_data, min_strength=0.5
                )
                if is_valid:
                    bonus = int(strength * 8)
                    vp_score += bonus
                    vp_info = "FVG+HVN"
                    reasons.append(f"⭐ VP: FVG dans HVN ({strength:.1f}x)")
            
            # BONUS 4: Position favorable (Discount/Premium selon Value Area)
            if direction == "BUY" and levels['bias'] == 'DISCOUNT_ZONE':
                vp_score += 5
                reasons.append("⭐ VP: Discount Zone")
            elif direction == "SELL" and levels['bias'] == 'PREMIUM_ZONE':
                vp_score += 5
                reasons.append("⭐ VP: Premium Zone")
            
            # BONUS 5: Volume Delta aligné avec direction
            if direction == "BUY" and levels.get('volume_pressure') == 'BULLISH':
                vp_score += 5
                reasons.append("⭐ VP: Volume Bullish")
            elif direction == "SELL" and levels.get('volume_pressure') == 'BEARISH':
                vp_score += 5
                reasons.append("⭐ VP: Volume Bearish")
            
            # BONUS 6: Prix près VAH/VAL (support/resistance institutionnel)
            if direction == "BUY" and abs(price - vp_data['val']) < atr * 0.3:
                vp_score += 5
                reasons.append("⭐ VP: Prix @ VAL")
            elif direction == "SELL" and abs(price - vp_data['vah']) < atr * 0.3:
                vp_score += 5
                reasons.append("⭐ VP: Prix @ VAH")
            
            vp_details = {
                'poc': vp_data['poc'],
                'vah': vp_data['vah'],
                'val': vp_data['val'],
                'vwap': vp_data['vwap'],
                'position': levels.get('position', 'N/A'),
                'volume_pressure': levels.get('volume_pressure', 'N/A'),
                'delta_ratio': vp_data['volume_delta_ratio']
            }
    
    score += vp_score  # Maximum +30 points bonus
    
    # ========================================
    # CLASSIFICATION & TARGETS
    # ========================================
    
    if score >= 95:
        quality = "INSTITUTIONAL ⭐⭐"
    elif score >= 90:
        quality = "ELITE 🏆"
    elif score >= 80:
        quality = "PREMIUM ⭐"
    else:
        quality = "STANDARD ✅"
    
    params = get_asset_params(symbol)
    sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
    tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
    
    details = {
        "quality": quality,
        "score": score,
        "reasons": reasons,
        "trigger": trigger_type,
        "midnight": f"{midnight_open:.5f}" if midnight_open else "N/A",
        "pdh_pdl": f"{pdh:.5f} / {pdl:.5f}" if pdh else "N/A",
        "zone_type": zone_text,
        "adx_h1": adx_h1,
        "adx_m5": adx_m5,
        "rsi_h1": rsi_h1_val,
        "rsi_m5": rsi_m5_current,
        "hma_m5": hma_m5_current,
        "vp_score": vp_score,
        "vp_info": vp_info,
        "vp_details": vp_details,
    }
    
    return score / 100, details, atr / price * 100, None, {'sl': sl, 'tp': tp}


@st.cache_data(ttl=3600, show_spinner=False)
def get_currency_strength_cached():
    """Currency Strength Matrix - Cached"""
    try:
        token = st.secrets["OANDA_ACCESS_TOKEN"]
        env = st.secrets.get("OANDA_ENVIRONMENT", "practice")
        client = oandapyV20.API(access_token=token, environment=env)
        
        pairs = [p for p in ASSETS if "_" in p and "XAU" not in p and "US30" not in p and "NAS100" not in p]
        prices = {}
        
        for p in pairs[:20]:
            try:
                params = {"count": 100, "granularity": "H1", "price": "M"}
                r = instruments.InstrumentsCandles(instrument=p, params=params)
                client.request(r)
                close_prices = [float(c['mid']['c']) for c in r.response['candles'] if c['complete']]
                if close_prices: 
                    prices[p] = pd.Series(close_prices)
            except:
                continue
        
        if not prices: 
            return None
        
        df_prices = pd.DataFrame(prices).ffill().bfill()
        
        def calc_rsi(series, period=14):
            delta = series.diff()
            gain = (delta.where(delta > 0, 0)).fillna(0)
            loss = (-delta.where(delta < 0, 0)).fillna(0)
            rs = gain.ewm(alpha=1/period, adjust=False).mean() / loss.ewm(alpha=1/period, adjust=False).mean()
            return 100 - (100 / (1 + rs))
        
        currencies = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]
        results = {}
        
        for curr in currencies:
            rsi_vals = []
            for col in df_prices.columns:
                base, quote = col.split('_')
                series = None
                
                if curr == base:
                    series = df_prices[col]
                elif curr == quote:
                    series = 1 / df_prices[col]
                
                if series is not None:
                    rsi = calc_rsi(series)
                    if len(rsi) > 1: 
                        rsi_vals.append(rsi.iloc[-1])
            
            if rsi_vals:
                rsi_avg = np.mean(rsi_vals)
                force = ((rsi_avg - 50) / 50 + 1) * 5
                results[curr] = {'force': round(force, 2)}
        
        return results
    except:
        return None


def scan_single_asset(args):
    """Scan parallelisé pour un asset"""
    sym, api, config = args
    try:
        df_m5 = api.get_candles(sym, "M5", 500)
        df_m15 = api.get_candles(sym, "M15", 200)
        df_h1 = api.get_candles(sym, "H1", 500)
        df_d = api.get_candles(sym, "D", 250)
        df_w = api.get_candles(sym, "W", 150)
        
        if df_m5.empty or df_m15.empty or df_h1.empty or df_d.empty or df_w.empty:
            return None
        
        live_price, spread = api.get_realtime_price_and_spread(sym)
        if live_price == 0:
            live_price = df_m5['close'].iloc[-1]
        
        cs_scores = get_currency_strength_cached()
        results = []
        
        for direction in ["BUY", "SELL"]:
            prob, details, atr_pct, reject, extras = calculate_signal_bluestar_v7(
                df_m5, df_m15, df_h1, df_d, df_w, sym, direction, live_price, cs_scores, config
            )
            
            if not reject and details['score'] >= config['min_score']:
                results.append({
                    'symbol': sym,
                    'type': direction,
                    'price': live_price,
                    'score': details['score'],
                    'details': details,
                    'sl': extras['sl'],
                    'tp': extras['tp'],
                    'rr': get_asset_params(sym)['tp_rr'],
                    'spread': spread
                })
        
        return results
    except Exception as e:
        return f"Error {sym}: {str(e)[:50]}"


def run_scan_bluestar_v7(api, config):
    """Scanner principal avec ThreadPool"""
    signals = []
    logs = []
    
    status = st.empty()
    status.info("🚀 BlueStar Sniper Ultimate 7 - Scan en cours...")
    
    args_list = [(sym, api, config) for sym in ASSETS]
    
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(scan_single_asset, args): args[0] for args in args_list}
        
        try:
            for future in as_completed(futures, timeout=180):
                try:
                    result = future.result(timeout=30)
                    if isinstance(result, list):
                        signals.extend(result)
                    elif isinstance(result, str):
                        logs.append(result)
                except Exception as e:
                    sym_name = futures.get(future, "Unknown")
                    logs.append(f"Error {sym_name}: {str(e)[:50]}")
        except Exception as e:
            # Si timeout global, on retourne ce qu'on a déjà
            logs.append(f"Scan timeout après 180s - {len(signals)} signaux trouvés")
    
    status.empty()
    return sorted(signals, key=lambda x: x['score'], reverse=True), logs


def display_signal_v7(s):
    """Affichage signal avec nouveau design"""
    is_buy = s['type'] == 'BUY'
    col_type = "#10b981" if is_buy else "#ef4444"
    bg = "linear-gradient(135deg, #064e3b 0%, #065f46 100%)" if is_buy else "linear-gradient(135deg, #7f1d1d 0%, #991b1b 100%)"
    d = s['details']
    
    if "ELITE" in d['quality']:
        badge = "<span class='badge badge-elite'>🏆 ELITE</span>"
    elif "INSTITUTIONAL" in d['quality']:
        badge = "<span class='badge' style='background:linear-gradient(135deg, #f59e0b 0%, #d97706 100%); animation: pulse-gold 2s infinite;'>⭐⭐ INSTITUTIONAL</span>"
    elif "PREMIUM" in d['quality']:
        badge = "<span class='badge badge-premium'>⭐ PREMIUM</span>"
    else:
        badge = "<span class='badge' style='background:#3b82f6;'>✅ STANDARD</span>"
    
    trigger_badge = f"<span class='badge badge-pirm'>🎯 {d['trigger']}</span>"
    
    with st.expander(f"{'📈' if is_buy else '📉'} **{s['symbol']}** | {s['type']} | Score: **{s['score']}/100**", expanded=True):
        st.markdown(f"""
        <div style="background:{bg};padding:18px;border-radius:12px;border:2px solid {col_type};margin-bottom:12px;box-shadow: 0 4px 15px rgba(0,0,0,0.3);">
            <span style="font-size:1.6em;font-weight:900;color:white;">{s['symbol']}</span>
            <span style="float:right;color:white;font-size:1.3em;font-weight:700;">{s['price']:.5f}</span><br>
            <div style='margin-top:12px;'>{badge} {trigger_badge}</div>
        </div>""", unsafe_allow_html=True)
        
        st.info(f"**Score:** {d['score']}/100 | **Zone:** {d['zone_type']} | **ADX H1:** {d['adx_h1']:.1f} | **ADX M5:** {d['adx_m5']:.1f} | **VP:** {d['vp_info']} (+{d['vp_score']})")
        
        st.markdown("### 📋 Confluences Validées")
        col1, col2 = st.columns(2)
        
        with col1:
            for i, reason in enumerate(d['reasons']):
                if i % 2 == 0:
                    if "🔥" in reason:
                        st.success(reason)
                    elif "✅" in reason:
                        st.success(reason)
                    else:
                        st.info(reason)
        
        with col2:
            for i, reason in enumerate(d['reasons']):
                if i % 2 == 1:
                    if "🔥" in reason:
                        st.success(reason)
                    elif "✅" in reason:
                        st.success(reason)
                    else:
                        st.info(reason)
        
        st.markdown("### 📊 Indicateurs Clés")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("RSI H1", f"{d['rsi_h1']:.1f}")
        c2.metric("RSI M5", f"{d['rsi_m5']:.1f}")
        
        # Premium/Discount Status
        midnight_val = d['midnight']
        if midnight_val != "N/A":
            try:
                mid_price = float(midnight_val)
                if s['price'] > mid_price:
                    c3.markdown(f"**Midnight**<br><span style='color:#ef4444;font-weight:700;'>🔴 PREMIUM</span><br><span style='font-size:0.8em;color:#64748b;'>{midnight_val}</span>", unsafe_allow_html=True)
                else:
                    c3.markdown(f"**Midnight**<br><span style='color:#10b981;font-weight:700;'>🟢 DISCOUNT</span><br><span style='font-size:0.8em;color:#64748b;'>{midnight_val}</span>", unsafe_allow_html=True)
            except:
                c3.metric("Midnight", midnight_val)
        else:
            c3.metric("Midnight", "N/A")
        
        c4.metric("PDH/PDL", d['pdh_pdl'])
        
        # Volume Profile Details
        if d['vp_details']:
            st.markdown("### 📈 Volume Profile (Institutional)")
            vp = d['vp_details']
            col_vp1, col_vp2, col_vp3, col_vp4 = st.columns(4)
            col_vp1.metric("POC", f"{vp['poc']:.5f}")
            col_vp2.metric("VAH", f"{vp['vah']:.5f}")
            col_vp3.metric("VAL", f"{vp['val']:.5f}")
            col_vp4.metric("Position", vp['position'].replace('_', ' '))
            
            col_vp5, col_vp6 = st.columns(2)
            vol_pressure = vp['volume_pressure']
            vol_color = "#10b981" if vol_pressure == "BULLISH" else ("#ef4444" if vol_pressure == "BEARISH" else "#94a3b8")
            col_vp5.markdown(f"**Volume Pressure:** <span style='color:{vol_color};font-weight:700;'>{vol_pressure}</span>", unsafe_allow_html=True)
            col_vp6.metric("Delta Ratio", f"{vp['delta_ratio']:.2%}")
        
        st.markdown("### 🎯 Niveaux de Trade")
        col_sl, col_tp = st.columns(2)
        col_sl.error(f"**🛑 STOP LOSS:** {s['sl']:.5f}")
        col_tp.success(f"**🎯 TAKE PROFIT:** {s['tp']:.5f} (RR: {s['rr']:.1f})")


def main():
    st.markdown("""
    <div class='header-container'>
        <span class='star-logo'>⭐</span><h1>BLUESTAR SNIPER ULTIMATE 7</h1>
    </div>
    <p style='text-align:center;color:#94a3b8;font-size:1.1em;margin-top:-10px;'>ULTIMATE FUSION: H1 Bias → M15 Alignment → M5 PIRM Trigger</p>
    """, unsafe_allow_html=True)
    
    with st.sidebar:
        st.markdown("<h2 style='color:#60a5fa;'>⚙️ Configuration</h2>", unsafe_allow_html=True)
        
        min_score = st.slider("🎯 Score Minimum", 60, 95, 75, 5,
            help="60-79: Standard | 80-89: Premium | 90+: Elite")
        
        st.markdown("---")
        st.markdown("<h3 style='color:#60a5fa;'>Filtres ADX Wilder</h3>", unsafe_allow_html=True)
        
        adx_enabled = st.checkbox("✅ Activer Filtre ADX", value=True)
        min_adx = st.slider("ADX H1 Minimum", 15, 30, 20, 1) if adx_enabled else 15
        
        st.markdown("---")
        st.markdown("<h3 style='color:#60a5fa;'>Zones & Confluences</h3>", unsafe_allow_html=True)
        
        use_zones = st.checkbox("🎯 Order Blocks / FVG", value=True,
            help="Active la détection des zones institutionnelles")
        
        use_vp = st.checkbox("📊 Volume Profile (Institutional)", value=True,
            help="Ajoute confluence Volume Profile - Niveau Hedge Fund")
        
        st.markdown("---")
        
        st.info("""
        **🎯 Système V7.0:**
        
        **Phase 1 - Biais H1:**
        - ADX Wilder > 20
        - Structure HH/HL ou LL/LH
        - HMA 50 + Position
        - RSI 14 > 50 (BUY)
        
        **Phase 2 - Alignement M15:**
        - HMA 20 alignée
        - Heiken Ashi confirmé
        - Currency Strength
        
        **Phase 3 - PIRM Trigger M5:**
        - Pullback HMA 20
        - RSI 10 OHLC4 Reload
        - Break niveau 50
        - ADX M5 > 18
        
        **Phase 4 - Zones (Bonus):**
        - Order Blocks
        - Fair Value Gaps
        - PDL/PDH
        
        **Phase 4.5 - Volume Profile:**
        - POC / VAH / VAL
        - HVN Confluence
        - Volume Delta
        - Institutional Levels
        
        **Score:**
        - 75-79: STANDARD ✅
        - 80-89: PREMIUM ⭐
        - 90-94: ELITE 🏆
        - 95+: INSTITUTIONAL ⭐⭐
        """)
    
    if st.button("🔍 SCAN ULTIMATE 7"):
        config = {
            'min_score': min_score,
            'min_adx': min_adx,
            'use_zones': use_zones,
            'use_vp': use_vp
        }
        
        with st.spinner("⚡ Scan parallèle en cours..."):
            api = OandaClient()
            results, logs = run_scan_bluestar_v7(api, config)
        
        if not results:
            st.warning("⚠️ Aucun signal PIRM validé pour le moment")
            
            if logs:
                with st.expander("📜 Logs d'Erreurs"):
                    for log in logs[:20]:
                        st.text(log)
        else:
            st.success(f"✅ **{len(results)} Signal(s) BlueStar Validé(s)**")
            
            # Statistiques
            elite = sum(1 for r in results if "ELITE" in r['details']['quality'])
            institutional = sum(1 for r in results if "INSTITUTIONAL" in r['details']['quality'])
            premium = sum(1 for r in results if "PREMIUM" in r['details']['quality'])
            standard = len(results) - elite - premium - institutional
            pirm = sum(1 for r in results if r['details']['trigger'] == "PIRM")
            
            col1, col2, col3, col4, col5 = st.columns(5)
            col1.metric("⭐⭐ Institutional", institutional)
            col2.metric("🏆 Elite", elite)
            col3.metric("⭐ Premium", premium)
            col4.metric("✅ Standard", standard)
            col5.metric("🎯 PIRM", pirm)
            
            st.markdown("---")
            
            for r in results:
                display_signal_v7(r)
            
            if logs:
                with st.expander("📜 Logs"):
                    for log in logs[:30]:
                        st.text(log)

if __name__ == "__main__":
    main()
