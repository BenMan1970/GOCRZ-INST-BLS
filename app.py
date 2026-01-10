import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
import logging, os, warnings
from concurrent.futures import ThreadPoolExecutor
import pytz

# --- CONFIGURATION ---
warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger().setLevel(logging.ERROR)
st.set_page_config(page_title="GOCRZ-Sniper PRO | MATRIX + PIRM", layout="centered", page_icon="🏦")

# --- CSS (TON DESIGN COMPLET) ---
st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700;900&display=swap');
* { font-family: 'Roboto', sans-serif; }
.stApp { background-color: #0f1117; background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%); }
h1 { background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 900; font-size: 2.2em; text-align: center; }
.stButton>button { width: 100%; border-radius: 12px; height: 3.5em; font-weight: 700; background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%); color: white; }
.badge-elite { background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); color: black; padding: 6px 12px; border-radius: 6px; font-weight: 700; }
.badge-premium { background: linear-gradient(135deg, #C0C0C0 0%, #808080 100%); color: black; padding: 6px 12px; border-radius: 6px; font-weight: 700; }
</style>""", unsafe_allow_html=True)

# --- CACHE ---
@st.cache_data(ttl=300, show_spinner=False)
def fetch_candles_cached(instrument, granularity, count):
    try:
        token = st.secrets["OANDA_ACCESS_TOKEN"]
        env = st.secrets.get("OANDA_ENVIRONMENT", "practice")
        client = oandapyV20.API(access_token=token, environment=env)
        params = {"count": count, "granularity": granularity, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)
        client.request(r)
        data = [{'time': pd.to_datetime(c['time']), 'open': float(c['mid']['o']), 'high': float(c['mid']['h']), 'low': float(c['mid']['l']), 'close': float(c['mid']['c']), 'volume': int(c['volume'])} for c in r.response['candles'] if c['complete']]
        return pd.DataFrame(data)
    except: return pd.DataFrame()

# --- CLIENT ---
class OandaClient:
    def __init__(self):
        try:
            self.access_token = st.secrets["OANDA_ACCESS_TOKEN"]
            self.account_id = st.secrets["OANDA_ACCOUNT_ID"]
            self.environment = st.secrets.get("OANDA_ENVIRONMENT", "practice")
            self.client = oandapyV20.API(access_token=self.access_token, environment=self.environment)
        except Exception as e:
            st.error(f"⚠️ API Error: {e}")
            st.stop()

    def get_candles(self, instrument, granularity, count):
        return fetch_candles_cached(instrument, granularity, count)

    def get_realtime_price_and_spread(self, instrument):
        try:
            r = pricing.PricingInfo(accountID=self.account_id, params={"instruments": instrument})
            self.client.request(r)
            p = r.response['prices'][0]
            pip = 100 if ("JPY" in instrument or "XAU" in instrument) else 10000
            if "US30" in instrument or "NAS100" in instrument: pip = 1
            return (float(p['closeoutBid']) + float(p['closeoutAsk'])) / 2, (float(p['closeoutAsk']) - float(p['closeoutBid'])) * pip
        except: return 0, 0

ASSETS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD", "EUR_GBP", "EUR_JPY", "GBP_JPY", "XAU_USD", "US30_USD", "NAS100_USD"]

def get_asset_params(symbol):
    if any(x in symbol for x in ["US30", "NAS100", "SPX500", "DE30"]): return {'sl_base': 2.0, 'tp_rr': 3.0}
    if any(x in symbol for x in ["XAU", "XAG"]): return {'sl_base': 1.8, 'tp_rr': 2.5}
    return {'sl_base': 1.5, 'tp_rr': 2.0}

# --- QUANT ENGINE (MATRICE + INSTITUTIONNEL) ---
class QuantEngine:
    @staticmethod
    def calculate_heikin_ashi(df):
        ha = df.copy()
        ha['ha_close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha['ha_open'] = 0.0
        ha_open = [df['open'].iloc[0]]
        for i in range(1, len(df)):
            ha_open.append((ha_open[-1] + ha['ha_close'].iloc[i-1]) / 2)
        ha['ha_open'] = ha_open
        return ha

    @staticmethod
    def calculate_atr_wilder(df, period=14):
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    @staticmethod
    def calculate_adx_wilder(df, period=14):
        up, down = df['high'].diff(), -df['low'].diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        smoothed_plus = pd.Series(plus_dm).ewm(alpha=1/period, adjust=False).mean()
        smoothed_minus = pd.Series(minus_dm).ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (smoothed_plus / atr)
        minus_di = 100 * (smoothed_minus / atr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        return dx.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    @staticmethod
    def calculate_hma(series, period):
        half, sqrt = int(period / 2), int(np.sqrt(period))
        wma_half = series.rolling(half).apply(lambda x: np.dot(x, np.arange(1, half+1)) / np.arange(1, half+1).sum(), raw=True)
        wma_full = series.rolling(period).apply(lambda x: np.dot(x, np.arange(1, period+1)) / np.arange(1, period+1).sum(), raw=True)
        return (2 * wma_half - wma_full).rolling(sqrt).apply(lambda x: np.dot(x, np.arange(1, sqrt+1)) / np.arange(1, sqrt+1).sum(), raw=True)

    @staticmethod
    def calculate_rsi_ohlc4(df, period=10):
        # RSI 10 OHLC4 (TA PRÉFÉRENCE POUR M5)
        ohlc4 = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        delta = ohlc4.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        rs = gain.ewm(alpha=1/period, adjust=False).mean() / loss.ewm(alpha=1/period, adjust=False).mean()
        return 100 - (100 / (1 + rs))

    @staticmethod
    def calculate_rsi_standard(series, period=14):
        # RSI 14 Close (POUR H1)
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        rs = gain.ewm(alpha=1/period, adjust=False).mean() / loss.ewm(alpha=1/period, adjust=False).mean()
        return 100 - (100 / (1 + rs))

    @staticmethod
    def detect_swing_structure(df, direction, lookback=50):
        if len(df) < lookback: return False
        if direction == "BUY": return df['low'].iloc[-1] >= df['low'].iloc[-lookback:].min()
        return df['high'].iloc[-1] <= df['high'].iloc[-lookback:].max()

    @staticmethod
    def get_midnight_open_ny(df):
        try:
            ny_tz = pytz.timezone('America/New_York')
            df_ny = df.copy()
            df_ny['time'] = pd.to_datetime(df_ny['time'], utc=True).dt.tz_convert(ny_tz)
            midnight = df_ny[df_ny['time'].dt.hour == 0]
            return midnight.iloc[-1]['open'] if not midnight.empty else None
        except: return None

    @staticmethod
    def get_pdh_pdl(df_d):
        return (df_d['high'].iloc[-2], df_d['low'].iloc[-2]) if len(df_d) >= 2 else (None, None)

# --- ENGINE FUSIONNÉ ---
def calculate_signal_pirm(df_m5, df_h1, df_d, symbol, direction, live_price, cs_scores, min_adx, use_zones):
    price = live_price
    atr = QuantEngine.calculate_atr_wilder(df_m5)
    midnight = QuantEngine.get_midnight_open_ny(df_m5)
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    
    # Indicateurs H1 (Trend)
    adx_h1 = QuantEngine.calculate_adx_wilder(df_h1, 14)
    hma50_h1 = QuantEngine.calculate_hma(df_h1['close'], 50).iloc[-1]
    rsi_h1 = QuantEngine.calculate_rsi_standard(df_h1['close'], 14).iloc[-1]
    price_h1 = df_h1['close'].iloc[-1]
    
    # Indicateurs M5 (Trigger Matrix)
    hma20_m5 = QuantEngine.calculate_hma(df_m5['close'], 20)
    rsi_m5_series = QuantEngine.calculate_rsi_ohlc4(df_m5, 10) # Ton RSI 10 OHLC4
    ha_m5 = QuantEngine.calculate_heikin_ashi(df_m5)
    
    if len(hma20_m5) < 5 or len(rsi_m5_series) < 5: return 0, {}, 0, "Data insuf.", {}
    
    # Valeurs actuelles
    hma_current = hma20_m5.iloc[-1]
    hma_prev = hma20_m5.iloc[-2]
    rsi_current = rsi_m5_series.iloc[-1]
    ha_close = ha_m5['ha_close'].iloc[-1]
    ha_open = ha_m5['ha_open'].iloc[-1]
    
    # Pente HMA M5 (La Matrice)
    hma_slope_up = hma_current > hma_prev
    
    reasons = []

    # ----------------------------------------
    # 1. FILTRE H1 : Tendance de fond
    # ----------------------------------------
    if adx_h1 < min_adx: return 0, {}, 0, f"ADX H1 Faible ({adx_h1:.1f})", {}
    
    if direction == "BUY":
        if price_h1 <= hma50_h1: return 0, {}, 0, "H1: Prix sous HMA 50", {}
        if rsi_h1 <= 50: return 0, {}, 0, "H1: RSI Bearish", {}
        if not QuantEngine.detect_swing_structure(df_h1, "BUY"): return 0, {}, 0, "H1: Structure Break", {}
    else: # SELL
        if price_h1 >= hma50_h1: return 0, {}, 0, "H1: Prix sur HMA 50", {}
        if rsi_h1 >= 50: return 0, {}, 0, "H1: RSI Bullish", {}
        if not QuantEngine.detect_swing_structure(df_h1, "SELL"): return 0, {}, 0, "H1: Structure Break", {}
        
    reasons.append(f"✅ H1 Tendance OK (ADX {adx_h1:.1f})")

    # ----------------------------------------
    # 2. FILTRE M5 : MATRICE (Couleur HMA)
    # ----------------------------------------
    if direction == "BUY":
        if not hma_slope_up: return 0, {}, 0, "❌ HMA 20 Rouge (M5)", {}
        # Protection HA (doit être vert)
        if ha_close <= ha_open: return 0, {}, 0, "❌ Heikin Ashi Rouge", {}
    else: # SELL
        if hma_slope_up: return 0, {}, 0, "❌ HMA 20 Verte (M5)", {}
        # Protection HA (doit être rouge)
        if ha_close >= ha_open: return 0, {}, 0, "❌ Heikin Ashi Vert", {}

    reasons.append("✅ Matrice M5 (HMA+HA) Alignée")

    # ----------------------------------------
    # 3. FILTRE ZONES & CS (Ta Matrice Complète)
    # ----------------------------------------
    if midnight:
        if direction == "BUY" and price > midnight and rsi_current > 65: return 0, {}, 0, "Premium (Prix > Midnight)", {}
        if direction == "SELL" and price < midnight and rsi_current < 35: return 0, {}, 0, "Discount (Prix < Midnight)", {}
    
    if pdh and pdl:
        if direction == "BUY" and price > pdh: return 0, {}, 0, "Above PDH", {}
        if direction == "SELL" and price < pdl: return 0, {}, 0, "Below PDL", {}
        
    if "_" in symbol and cs_scores:
        base, quote = symbol.split('_')
        b = cs_scores.get(base, {'force': 5.0})
        q = cs_scores.get(quote, {'force': 5.0})
        gap = b['force'] - q['force']
        if direction == "BUY" and gap < 0.5: return 0, {}, 0, f"CS Gap Faible ({gap:.1f})", {}
        if direction == "SELL" and gap > -0.5: return 0, {}, 0, f"CS Gap Faible ({gap:.1f})", {}
        reasons.append(f"✅ CS Gap: {gap:.1f}")

    # ----------------------------------------
    # 4. LE TRIGGER INSTITUTIONNEL (PIRM)
    # ----------------------------------------
    # On cherche le "Dip" (Creux) récent du RSI
    prev_rsi = rsi_m5_series.iloc[-4:-1] # Les 3 bougies avant l'actuelle
    
    trigger_valid = False
    dist_hma = (price - hma_current) / hma_current if direction == "BUY" else (hma_current - price) / hma_current
    
    if direction == "BUY":
        # 1. Contact récent avec HMA 20 (Pullback)
        touched_hma = (df_m5['low'].iloc[-4:] <= hma_current * 1.0005).any()
        if not touched_hma: return 0, {}, 0, "M5: Pas de Pullback HMA", {}
        
        # 2. Séquence RSI 10 : Reload Zone (40-55) -> Break 50
        # On vérifie si RSI a visité la zone 40-55 récemment
        rsi_dipped = ((prev_rsi >= 40) & (prev_rsi <= 55)).any()
        
        if rsi_dipped and rsi_current > 50:
            # 3. Proximité HMA (Sniper)
            if dist_hma < 0.003: 
                trigger_valid = True
        elif not rsi_dipped:
             return 0, {}, 0, "M5: Pas de Reload RSI (40-55)", {}
             
    else: # SELL
        # 1. Contact HMA
        touched_hma = (df_m5['high'].iloc[-4:] >= hma_current * 0.9995).any()
        if not touched_hma: return 0, {}, 0, "M5: Pas de Pullback HMA", {}
        
        # 2. RSI Sequence: Zone 45-60 -> Break 50
        rsi_dipped = ((prev_rsi <= 60) & (prev_rsi >= 45)).any()
        
        if rsi_dipped and rsi_current < 50:
            if dist_hma < 0.003:
                trigger_valid = True
        elif not rsi_dipped:
            return 0, {}, 0, "M5: Pas de Reload RSI (45-60)", {}

    if not trigger_valid: return 0, {}, 0, f"Trigger Invalide (RSI {rsi_current:.1f})", {}
    
    reasons.append(f"🔥 PIRM TRIGGER: RSI Dip + Break 50")
    
    # SCORING & TARGETS
    score = 85
    if abs(dist_hma) < 0.001: score += 10
    
    quality = "ELITE 🏆" if score >= 90 else "PREMIUM ⭐"
    params = get_asset_params(symbol)
    sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
    tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
    
    info = {
        'quality': quality, 'score': score, 'reasons': reasons,
        'midnight': f"{midnight:.5f}" if midnight else "N/A",
        'pdh_pdl': f"{pdh:.5f}/{pdl:.5f}" if pdh else "N/A",
        'adx': adx_h1,
        'pirm': {'rsi': rsi_current, 'hma': hma_current}
    }
    
    return 1.0, info, atr / price * 100, None, {'sl': sl, 'tp': tp}

# --- CURRENCY STRENGTH (CACHE) ---
@st.cache_data(ttl=3600)
def get_currency_strength_rsi_cached():
    try:
        token = st.secrets["OANDA_ACCESS_TOKEN"]
        client = oandapyV20.API(access_token=token, environment=st.secrets.get("OANDA_ENVIRONMENT", "practice"))
        pairs = [p for p in ASSETS if "_" in p and "XAU" not in p and "US30" not in p]
        prices = {}
        for p in pairs[:15]:
            params = {"count": 100, "granularity": "H1", "price": "M"}
            r = instruments.InstrumentsCandles(instrument=p, params=params)
            client.request(r)
            close_prices = [float(c['mid']['c']) for c in r.response['candles'] if c['complete']]
            if close_prices: prices[p] = pd.Series(close_prices)
        if not prices: return None
        df_prices = pd.DataFrame(prices).ffill().bfill()
        
        def calc_rsi(series, period=14):
            delta = series.diff()
            gain = (delta.where(delta > 0, 0)).fillna(0)
            loss = (-delta.where(delta < 0, 0)).fillna(0)
            rs = gain.ewm(alpha=1/period, adjust=False).mean() / loss.ewm(alpha=1/period, adjust=False).mean()
            return 100 - (100 / (1 + rs))
        
        currencies = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD"]
        results = {}
        for curr in currencies:
            rsi_vals = []
            for col in df_prices.columns:
                base, quote = col.split('_')
                series = df_prices[col] if curr == base else (1 / df_prices[col] if curr == quote else None)
                if series is not None:
                    rsi = calc_rsi(series)
                    if len(rsi) > 1: rsi_vals.append(rsi.iloc[-1])
            if rsi_vals:
                rsi_avg = np.mean(rsi_vals)
                force = ((rsi_avg - 50) / 50 + 1) * 5
                results[curr] = {'force': round(force, 2)}
        return results
    except: return None

# --- SCANNER ---
def run_scan_pirm(api, min_score, min_adx, use_zones):
    cs_scores = get_currency_strength_rsi_cached()
    signals, logs = [], []
    status = st.empty()
    
    def scan_asset(sym):
        try:
            df_m5 = api.get_candles(sym, "M5", 500)
            df_h1 = api.get_candles(sym, "H1", 500)
            df_d = api.get_candles(sym, "D", 5)
            if df_m5.empty or df_h1.empty: return None
            
            live_price = df_m5['close'].iloc[-1]
            res_list = []
            for direction in ["BUY", "SELL"]:
                prob, info, atr_pct, reject, extras = calculate_signal_pirm(
                    df_m5, df_h1, df_d, sym, direction, live_price, cs_scores, min_adx, use_zones
                )
                if not reject and info['score'] >= min_score:
                    _, spread = api.get_realtime_price_and_spread(sym)
                    res_list.append({
                        'symbol': sym, 'type': direction, 'price': live_price, 'score': info['score'],
                        'details': info, 'sl': extras['sl'], 'tp': extras['tp'], 'spread': spread
                    })
            return res_list
        except Exception as e: return f"Err {sym}: {str(e)[:40]}"

    status.info("🚀 Scan PIRM MATRIX en cours...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        results_future = list(executor.map(scan_asset, ASSETS))

    for res in results_future:
        if isinstance(res, list): signals.extend(res)
        elif isinstance(res, str): logs.append(res)
            
    status.empty()
    return sorted(signals, key=lambda x: x['score'], reverse=True), logs

# --- AFFICHAGE ---
def display_signal(s):
    col_type = "#10b981" if s['type'] == "BUY" else "#ef4444"
    bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if s['type'] == "BUY" else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
    d = s['details']
    badge = "<span class='badge-elite'>🏆 ELITE</span>" if "ELITE" in d['quality'] else "<span class='badge-premium'>⭐ PREMIUM</span>"
    
    with st.expander(f"{'📈' if s['type'] == 'BUY' else '📉'} {s['symbol']} | {s['type']} | Score: {s['score']}", expanded=True):
        st.markdown(f"""<div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};">
        <span style="font-size:1.5em;font-weight:900;color:white;">{s['symbol']}</span>
        <span style="float:right;color:white;font-size:1.2em;">{s['price']:.5f}</span><br>
        <div style='margin-top:10px;'>{badge}</div></div>""", unsafe_allow_html=True)
        
        st.info(f"**Score:** {d['score']}/100 | **ADX:** {d['adx']:.1f} | **Midnight:** {d['midnight']}")
        for r in d['reasons']:
            if "🔥" in r: st.success(r)
            elif "✅" in r: st.success(r)
        
        c1, c2 = st.columns(2)
        c1.info(f"🛑 SL: {s['sl']:.5f}")
        c2.success(f"🎯 TP: {s['tp']:.5f}")

# --- MAIN ---
def main():
    st.title("🎯 GOCRZ-Sniper PRO | MATRIX + PIRM")
    with st.sidebar:
        st.header("⚙️ Paramètres")
        min_score = st.slider("Score Min", 60, 95, 75, 5)
        min_adx = st.slider("ADX Min (Wilder)", 15, 35, 20, 1)
        use_zones = st.checkbox("🎯 Zones (Discount/Premium)", value=True)
    
    if st.button("🔍 SCANNER"):
        api = OandaClient()
        results, logs = run_scan_pirm(api, min_score, min_adx, use_zones)
        if not results: st.warning("⚠️ Aucun signal 'Perfect' (Matrice + Trigger).")
        else:
            st.success(f"✅ {len(results)} Signal(s)")
            for r in results: display_signal(r)
        if logs:
            with st.expander("Logs"):
                for log in logs: st.text(log)

if __name__ == "__main__":
    main()
