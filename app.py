import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
import logging, os, warnings
from datetime import datetime, timedelta
import pytz
from concurrent.futures import ThreadPoolExecutor

# --- CONFIGURATION ---
warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger().setLevel(logging.ERROR)
st.set_page_config(page_title="GOCRZ-Sniper PRO | PIRM-5M", layout="centered", page_icon="🎯")

# --- CSS (INTOUCHÉ) ---
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
        
        # Astuce Oanda: On demande "M" (Midpoint) pour avoir OHLC
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

# --- API CLIENT ---
class OandaClient:
    def __init__(self):
        try:
            self.access_token = st.secrets["OANDA_ACCESS_TOKEN"]
            self.account_id = st.secrets["OANDA_ACCOUNT_ID"]
            self.environment = st.secrets.get("OANDA_ENVIRONMENT", "practice")
            self.client = oandapyV20.API(access_token=self.access_token, environment=self.environment)
        except Exception as e:
            st.error(f"⚠️ Config API: {e}")
            st.stop()

    def get_candles(self, instrument, granularity, count):
        return fetch_candles_cached(instrument, granularity, count)

    def get_realtime_price_and_spread(self, instrument):
        try:
            r = pricing.PricingInfo(accountID=self.account_id, params={"instruments": instrument})
            self.client.request(r)
            price = r.response['prices'][0]
            bid, ask = float(price['closeoutBid']), float(price['closeoutAsk'])
            
            pip_mult = 100 if ("JPY" in instrument or "XAU" in instrument) else 10000
            if any(x in instrument for x in ["US30", "NAS100", "SPX500", "DE30"]): pip_mult = 1
            
            return (bid + ask) / 2, (ask - bid) * pip_mult
        except: return 0, 0

ASSETS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD", "EUR_GBP", "EUR_JPY", "GBP_JPY", "XAU_USD", "US30_USD", "NAS100_USD"]

def get_asset_params(symbol):
    if any(x in symbol for x in ["US30", "NAS100", "SPX500", "DE30"]): return {'sl_base': 2.0, 'tp_rr': 3.0}
    if any(x in symbol for x in ["XAU", "XAG"]): return {'sl_base': 1.8, 'tp_rr': 2.5}
    return {'sl_base': 1.5, 'tp_rr': 2.0}

# --- QUANT ENGINE (CORRIGÉ & CALIBRÉ TV) ---
class QuantEngine:
    @staticmethod
    def calculate_atr_wilder(df, period=14):
        # ATR Wilder (RMA)
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        # alpha=1/period correspond exactement au lissage RMA de TradingView
        return tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    @staticmethod
    def calculate_adx_wilder(df, period=14):
        # ADX Wilder (Exactitude TradingView)
        high, low, close = df['high'], df['low'], df['close']
        
        up = high.diff()
        down = -low.diff()
        
        # 1. Directional Movement (DM)
        plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
        
        # 2. True Range
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        
        # 3. Smoothing (RMA) - C'est ici que l'historique compte !
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        smoothed_plus = plus_dm.ewm(alpha=1/period, adjust=False).mean()
        smoothed_minus = minus_dm.ewm(alpha=1/period, adjust=False).mean()
        
        # 4. DI
        plus_di = 100 * (smoothed_plus / atr)
        minus_di = 100 * (smoothed_minus / atr)
        
        # 5. DX & ADX
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        adx = dx.ewm(alpha=1/period, adjust=False).mean()
        
        return adx.iloc[-1]

    @staticmethod
    def calculate_hma(series, period=20):
        # HMA Standard
        half, sqrt = int(period / 2), int(np.sqrt(period))
        wma_half = series.rolling(half).apply(lambda x: np.dot(x, np.arange(1, half+1)) / np.arange(1, half+1).sum(), raw=True)
        wma_full = series.rolling(period).apply(lambda x: np.dot(x, np.arange(1, period+1)) / np.arange(1, period+1).sum(), raw=True)
        diff = 2 * wma_half - wma_full
        return diff.rolling(sqrt).apply(lambda x: np.dot(x, np.arange(1, sqrt+1)) / np.arange(1, sqrt+1).sum(), raw=True)

    @staticmethod
    def calculate_rsi_ohlc4(df, period=10):
        # RSI 10 sur OHLC4
        ohlc4 = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        delta = ohlc4.diff()
        
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        
        # Lissage RMA pour le RSI (Standard)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        
        rs = avg_gain / avg_loss.replace(0, 0.0001)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def detect_swing_structure(df, direction, lookback=30):
        if len(df) < lookback: return False
        highs, lows = df['high'].iloc[-lookback:], df['low'].iloc[-lookback:]
        
        if direction == "BUY":
            # Pas de lower low récent
            return lows.iloc[-1] >= lows.iloc[-lookback:].min()
        else:
            # Pas de higher high récent
            return highs.iloc[-1] <= highs.iloc[-lookback:].max()

    @staticmethod
    def get_midnight_open_ny(df):
        try:
            ny_tz = pytz.timezone('America/New_York')
            # Conversion de la dernière bougie pour avoir la date courante à NY
            last_dt = pd.to_datetime(df['time'].iloc[-1], utc=True).astimezone(ny_tz)
            
            # On définit le Minuit qu'on cherche
            midnight_time = last_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            
            df_ny = df.copy()
            df_ny['dt_ny'] = pd.to_datetime(df_ny['time'], utc=True).dt.tz_convert(ny_tz)
            
            # On cherche les bougies AVANT ou ÉGALE à minuit
            candidates = df_ny.loc[df_ny['dt_ny'] <= midnight_time]
            
            if not candidates.empty:
                # La dernière candidate est celle de minuit (ou 23h55 la veille si gap)
                return candidates.iloc[-1]['open']
            return None
        except: return None

    @staticmethod
    def get_pdh_pdl(df_d):
        return (df_d['high'].iloc[-2], df_d['low'].iloc[-2]) if len(df_d) >= 2 else (None, None)

# --- CALCUL DU SIGNAL ---
def calculate_signal_pirm(df_m5, df_h1, df_d, symbol, direction, live_price, cs_scores, min_adx, use_zones):
    price = live_price
    atr = QuantEngine.calculate_atr_wilder(df_m5)
    midnight = QuantEngine.get_midnight_open_ny(df_m5)
    
    adx_h1 = QuantEngine.calculate_adx_wilder(df_h1, 14)
    rsi_m5 = QuantEngine.calculate_rsi_ohlc4(df_m5, 10)
    hma_m5 = QuantEngine.calculate_hma(df_m5['close'], 20)
    
    reasons = []
    
    # 1. H1 CHECK
    if adx_h1 < min_adx: return 0, {}, 0, f"ADX H1 {adx_h1:.1f} < {min_adx}", {}
    
    if not QuantEngine.detect_swing_structure(df_h1, direction):
        return 0, {}, 0, "Contre Structure H1", {}
    reasons.append(f"✅ H1 Validé (ADX {adx_h1:.1f})")
    
    # 2. ZONES (Midnight & PDH/PDL)
    if midnight:
        if direction == "BUY":
            # Achat : Attention si Prix >> Midnight (Premium)
            # Tolérance : Si RSI est fort (>65), on accepte d'acheter cher
            if price > midnight and rsi_m5 > 65: return 0, {}, 0, "Prix Premium (Trop haut)", {}
        else:
            # Vente : Attention si Prix << Midnight (Discount)
            if price < midnight and rsi_m5 < 35: return 0, {}, 0, "Prix Discount (Trop bas)", {}
    
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    if pdh and pdl:
        if direction == "BUY" and price > pdh: return 0, {}, 0, "Au-dessus PDH", {}
        if direction == "SELL" and price < pdl: return 0, {}, 0, "En-dessous PDL", {}
    
    reasons.append("✅ Zones Discount/Premium OK")
    
    # 3. CURRENCY STRENGTH
    if "_" in symbol and cs_scores:
        base, quote = symbol.split('_')
        b = cs_scores.get(base, {'force': 5.0, 'coherence': 0.0})
        q = cs_scores.get(quote, {'force': 5.0, 'coherence': 0.0})
        gap = b['force'] - q['force']
        
        if direction == "BUY" and gap < 0.5: return 0, {}, 0, f"CS Faible ({gap:.1f})", {}
        if direction == "SELL" and gap > -0.5: return 0, {}, 0, f"CS Faible ({gap:.1f})", {}
        reasons.append(f"✅ CS Gap: {gap:.1f}")

    # 4. TRIGGER PIRM-5M
    if len(hma_m5) < 3: return 0, {}, 0, "Données insuf.", {}
    hma_val = hma_m5.iloc[-2]
    rsi_val = rsi_m5.iloc[-2]
    
    trigger_valid = False
    dist_hma = (price - hma_val) / hma_val if direction == "BUY" else (hma_val - price) / hma_val
    
    if direction == "BUY":
        # RSI sain pour un achat & Prix proche HMA
        if 40 <= rsi_val <= 65:
            if -0.001 < dist_hma < 0.0025: trigger_valid = True
    else:
        # RSI sain pour une vente & Prix proche HMA
        if 35 <= rsi_val <= 60:
            if -0.001 < dist_hma < 0.0025: trigger_valid = True
            
    if not trigger_valid: return 0, {}, 0, f"Trigger Invalide (RSI {rsi_val:.1f})", {}
    
    reasons.append(f"🔥 PIRM-5M: RSI {rsi_val:.1f} | Dist.HMA {dist_hma*100:.3f}%")
    
    # SCORING
    score = 80
    if abs(dist_hma) < 0.001: score += 10
    if adx_h1 > 30: score += 5
    
    quality = "ELITE 🏆" if score >= 85 else "PREMIUM ⭐"
    
    params = get_asset_params(symbol)
    sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
    tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
    
    info = {
        'quality': quality, 'score': score, 'reasons': reasons,
        'midnight': f"{midnight:.5f}" if midnight else "N/A",
        'pdh_pdl': f"{pdh:.5f}/{pdl:.5f}" if pdh else "N/A",
        'adx': adx_h1,
        'pirm': {'rsi': rsi_val, 'hma': hma_val}
    }
    
    return 1.0, info, atr / price * 100, None, {'sl': sl, 'tp': tp}

# --- CURRENCY STRENGTH ---
@st.cache_data(ttl=3600)
def get_currency_strength_rsi_cached():
    try:
        token = st.secrets["OANDA_ACCESS_TOKEN"]
        client = oandapyV20.API(access_token=token, environment=st.secrets.get("OANDA_ENVIRONMENT", "practice"))
        
        pairs = [p for p in ASSETS if "_" in p and "XAU" not in p and "US30" not in p]
        prices = {}
        # On ne charge que 100 bougies ici, suffisant pour un RSI H1
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
            avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
            return 100 - (100 / (1 + avg_gain / avg_loss.replace(0, 0.0001)))
        
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
                coherence = abs((sum(1 for r in rsi_vals if r > 50) - sum(1 for r in rsi_vals if r < 50)) / len(rsi_vals))
                results[curr] = {'force': round(force, 2), 'coherence': round(coherence, 2)}
        return results
    except: return None

# --- SCANNER ---
def run_scan_pirm(api, min_score, min_adx, use_zones):
    cs_scores = get_currency_strength_rsi_cached()
    signals, logs = [], []
    status = st.empty()
    
    def scan_asset(sym):
        try:
            # === MODIFICATION MAJEURE ===
            # Augmentation drastique de l'historique pour précision ADX et Midnight
            df_m5 = api.get_candles(sym, "M5", 500) # 500 bougies = ~41 heures -> Midnight garanti
            df_h1 = api.get_candles(sym, "H1", 500) # 500 bougies = Stabilité ADX garantie
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
        except Exception as e:
            return f"Err {sym}: {str(e)[:20]}"

    status.info("🚀 Scan PIRM en cours (Haute Précision)...")
    
    with ThreadPoolExecutor(max_workers=8) as executor:
        results_future = list(executor.map(scan_asset, ASSETS))

    for res in results_future:
        if isinstance(res, list):
            signals.extend(res)
        elif isinstance(res, str):
            logs.append(res)
            
    status.empty()
    return sorted(signals, key=lambda x: x['score'], reverse=True), logs

# --- AFFICHAGE ---
def display_signal(s):
    col_type = "#10b981" if s['type'] == "BUY" else "#ef4444"
    bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if s['type'] == "BUY" else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
    d = s['details']
    
    badge = "<span class='badge-elite'>🏆 ELITE</span>" if "ELITE" in d['quality'] else "<span class='badge-premium'>⭐ PREMIUM</span>" if "PREMIUM" in d['quality'] else "✅ STANDARD"
    
    with st.expander(f"{'📈' if s['type'] == 'BUY' else '📉'} {s['symbol']} | {s['type']} | Score: {s['score']}", expanded=True):
        st.markdown(f"""<div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};">
        <span style="font-size:1.5em;font-weight:900;color:white;">{s['symbol']}</span>
        <span style="float:right;color:white;font-size:1.2em;">{s['price']:.5f}</span><br>
        <div style='margin-top:10px;'>{badge}</div></div>""", unsafe_allow_html=True)
        
        st.info(f"**Score:** {d['score']}/100 | **ADX:** {d['adx']:.1f} | **Midnight:** {d['midnight']}")
        
        st.markdown("### 🎯 Critères PIRM-5M")
        for r in d['reasons']:
            if "🔥" in r: st.success(r)
            elif "✅" in r: st.success(r)
        
        pirm = d.get('pirm', {})
        if pirm:
            st.markdown(f"**RSI OHLC4:** {pirm.get('rsi', 0):.1f} | **HMA 20:** {pirm.get('hma', 0):.5f}")
        
        c1, c2 = st.columns(2)
        c1.info(f"🛑 SL: {s['sl']:.5f}")
        c2.success(f"🎯 TP: {s['tp']:.5f}")

# --- MAIN ---
def main():
    st.title("🎯 GOCRZ-Sniper PRO | PIRM-5M")
    
    with st.sidebar:
        st.header("⚙️ Paramètres")
        min_score = st.slider("Score Min", 60, 95, 75, 5)
        min_adx = st.slider("ADX Min (Wilder)", 15, 35, 20, 1)
        use_zones = st.checkbox("🎯 Zones (Discount/Premium)", value=True)
    
    if st.button("🔍 SCANNER PIRM-5M"):
        api = OandaClient()
        results, logs = run_scan_pirm(api, min_score, min_adx, use_zones)
        
        if not results:
            st.warning("⚠️ Aucun signal PIRM-5M valide")
        else:
            st.success(f"✅ {len(results)} Signal(s) PIRM-5M")
            elite = sum(1 for r in results if "ELITE" in r['details']['quality'])
            premium = sum(1 for r in results if "PREMIUM" in r['details']['quality'])
            c1, c2, c3 = st.columns(3)
            c1.metric("🏆 Elite", elite)
            c2.metric("⭐ Premium", premium)
            c3.metric("✅ Total", len(results))
            
            for r in results: display_signal(r)
            
        if logs:
            with st.expander("📜 Logs erreurs"):
                for log in logs: st.text(log)

if __name__ == "__main__":
    main()
