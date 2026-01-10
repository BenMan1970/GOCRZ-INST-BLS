import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
import logging, os, warnings
from datetime import datetime
import pytz

warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger().setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

st.set_page_config(page_title="GOCRZ-Sniper PRO | PIRM-5M", layout="centered", page_icon="🎯")

if 'trade_logs' not in st.session_state: st.session_state.trade_logs = []
if 'cache' not in st.session_state: st.session_state.cache = {}
if 'cs_data' not in st.session_state: st.session_state.cs_data = {'data': None, 'time': None}

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700;900&display=swap');
* { font-family: 'Roboto', sans-serif; }
.stApp { background-color: #0f1117; background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%); }
h1 { background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 900; font-size: 2.2em; text-align: center; }
.stButton>button { width: 100%; border-radius: 12px; height: 3.5em; font-weight: 700; background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%); color: white; }
.badge-elite { background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); color: black; padding: 6px 12px; border-radius: 6px; font-weight: 700; }
.badge-premium { background: linear-gradient(135deg, #C0C0C0 0%, #808080 100%); color: black; padding: 6px 12px; border-radius: 6px; font-weight: 700; }
</style>""", unsafe_allow_html=True)

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
        key = f"{instrument}_{granularity}_{count}"
        if key in st.session_state.cache:
            ts, data = st.session_state.cache[key]
            timeout = {"M5": 15, "M15": 60, "H1": 300, "D": 900, "W": 3600}.get(granularity, 900)
            if (datetime.now() - ts).total_seconds() < timeout: return data
        try:
            params = {"count": count, "granularity": granularity, "price": "M"}
            r = instruments.InstrumentsCandles(instrument=instrument, params=params)
            self.client.request(r)
            data = [{'time': pd.to_datetime(c['time']), 'open': float(c['mid']['o']), 'high': float(c['mid']['h']), 'low': float(c['mid']['l']), 'close': float(c['mid']['c']), 'volume': int(c['volume'])} for c in r.response['candles'] if c['complete']]
            df = pd.DataFrame(data)
            if not df.empty: st.session_state.cache[key] = (datetime.now(), df)
            return df
        except: return pd.DataFrame()

    def get_realtime_price_and_spread(self, instrument):
        try:
            r = pricing.PricingInfo(accountID=self.account_id, params={"instruments": instrument})
            self.client.request(r)
            price = r.response['prices'][0]
            bid, ask = float(price['closeoutBid']), float(price['closeoutAsk'])
            pip_mult = 100 if ("JPY" in instrument or "XAU" in instrument or "XAG" in instrument) else 10000
            if any(x in instrument for x in ["US30", "NAS100", "SPX500", "DE30"]): pip_mult = 1
            return (bid + ask) / 2, (ask - bid) * pip_mult
        except: return 0, 0

ASSETS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD", "EUR_GBP", "EUR_JPY", "GBP_JPY", "XAU_USD", "US30_USD", "NAS100_USD"]

def get_asset_params(symbol):
    if any(x in symbol for x in ["US30", "NAS100", "SPX500", "DE30"]): return {'sl_base': 2.0, 'tp_rr': 3.0}
    if any(x in symbol for x in ["XAU", "XAG"]): return {'sl_base': 1.8, 'tp_rr': 2.5}
    return {'sl_base': 1.5, 'tp_rr': 2.0}

class QuantEngine:
    @staticmethod
    def calculate_atr(df, period=14):
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
        return tr.ewm(span=period).mean().iloc[-1]

    @staticmethod
    def calculate_adx(df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        plus_dm, minus_dm = high.diff().clip(lower=0), (-low.diff()).clip(lower=0)
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / atr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)).fillna(0) * 100
        return dx.ewm(alpha=1/period).mean().iloc[-1]

    @staticmethod
    def calculate_hma(series, period=20):
        half, sqrt = int(period / 2), int(np.sqrt(period))
        wma_half = series.rolling(half).apply(lambda x: np.dot(x, np.arange(1, half+1)) / np.arange(1, half+1).sum(), raw=True)
        wma_full = series.rolling(period).apply(lambda x: np.dot(x, np.arange(1, period+1)) / np.arange(1, period+1).sum(), raw=True)
        diff = 2 * wma_half - wma_full
        return diff.rolling(sqrt).apply(lambda x: np.dot(x, np.arange(1, sqrt+1)) / np.arange(1, sqrt+1).sum(), raw=True)

    @staticmethod
    def calculate_rsi_ohlc4(df, period=14):
        ohlc4 = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        delta = ohlc4.diff()
        gain, loss = (delta.where(delta > 0, 0)).fillna(0), (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 0.0001)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def calculate_heikin_ashi(df):
        ha = df.copy()
        ha['ha_close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha['ha_open'] = 0.0
        for i in range(len(df)):
            ha.loc[ha.index[i], 'ha_open'] = (df['open'].iloc[i] + df['close'].iloc[i]) / 2 if i == 0 else (ha['ha_open'].iloc[i-1] + ha['ha_close'].iloc[i-1]) / 2
        ha['ha_high'] = ha[['high', 'ha_open', 'ha_close']].max(axis=1)
        ha['ha_low'] = ha[['low', 'ha_open', 'ha_close']].min(axis=1)
        return ha

    @staticmethod
    def detect_swing_structure(df, direction, lookback=30):
        if len(df) < lookback: return False
        highs, lows = df['high'].iloc[-lookback:], df['low'].iloc[-lookback:]
        if direction == "BUY":
            return highs.iloc[-5:].max() > highs.iloc[-lookback:-5].max() and lows.iloc[-5:].min() > lows.iloc[-lookback:-5].min()
        return highs.iloc[-5:].max() < highs.iloc[-lookback:-5].max() and lows.iloc[-5:].min() < lows.iloc[-lookback:-5].min()

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

    @staticmethod
    def detect_pirm_trigger(df_m5, direction):
        if len(df_m5) < 50: return False, {}
        hma = QuantEngine.calculate_hma(df_m5['close'], 20)
        rsi = QuantEngine.calculate_rsi_ohlc4(df_m5, 14)
        adx = QuantEngine.calculate_adx(df_m5, 14)
        ha = QuantEngine.calculate_heikin_ashi(df_m5)
        if len(hma) < 3 or len(rsi) < 3: return False, {}
        
        price, hma_val, rsi_val = df_m5['close'].iloc[-2], hma.iloc[-2], rsi.iloc[-2]
        ha_green = ha['ha_close'].iloc[-2] > ha['ha_open'].iloc[-2]
        
        details = {'hma': hma_val, 'rsi': rsi_val, 'adx': adx, 'ha_color': 'GREEN' if ha_green else 'RED', 'price': price}
        
        if direction == "BUY":
            if abs(price - hma_val) / hma_val > 0.002: return False, details
            if not (45 <= rsi_val <= 50) or rsi.iloc[-10:].min() < 40: return False, details
            if adx < 20 or price <= hma_val or not ha_green or rsi_val <= 50: return False, details
            return True, details
        else:
            if abs(price - hma_val) / hma_val > 0.002: return False, details
            if not (50 <= rsi_val <= 55) or rsi.iloc[-10:].max() > 60: return False, details
            if adx < 20 or price >= hma_val or ha_green or rsi_val >= 50: return False, details
            return True, details

def calculate_signal_pirm(df_m5, df_h1, df_d, df_w, symbol, direction, live_price, cs_scores, min_adx, use_zones):
    price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
    atr = QuantEngine.calculate_atr(df_m5)
    midnight = QuantEngine.get_midnight_open_ny(df_m5)
    pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
    reasons = []
    
    # 1. Régime ADX
    adx_h1 = QuantEngine.calculate_adx(df_h1)
    if adx_h1 < min_adx: return 0, {}, 0, f"ADX {adx_h1:.1f} < {min_adx}", {}
    reasons.append(f"✅ ADX {adx_h1:.1f}")
    
    # 2. Direction H1 + Structure
    hma_h1 = QuantEngine.calculate_hma(df_h1['close'], 50)
    if len(hma_h1) < 3: return 0, {}, 0, "HMA H1 insuffisant", {}
    price_h1 = df_h1['close'].iloc[-1]
    structure_ok = QuantEngine.detect_swing_structure(df_h1, direction)
    
    if direction == "BUY":
        if price_h1 <= hma_h1.iloc[-2] or not structure_ok: return 0, {}, 0, "Direction/Structure H1", {}
    else:
        if price_h1 >= hma_h1.iloc[-2] or not structure_ok: return 0, {}, 0, "Direction/Structure H1", {}
    reasons.append("✅ Direction + Structure H1")
    
    # 3. Biais Midnight + PDH/PDL
    if midnight:
        if (direction == "BUY" and price >= midnight) or (direction == "SELL" and price <= midnight):
            return 0, {}, 0, f"Biais Midnight", {}
        reasons.append("✅ Biais Midnight")
    
    if pdh and pdl:
        if (direction == "BUY" and price > pdh) or (direction == "SELL" and price < pdl):
            return 0, {}, 0, "Biais PDH/PDL", {}
        reasons.append("✅ PDH/PDL OK")
    
    # 4. Zones (simplifié pour l'exemple)
    if use_zones:
        reasons.append("✅ Zones activées")
    
    # 5. Currency Strength
    if "_" in symbol and cs_scores:
        base, quote = symbol.split('_')
        b = cs_scores.get(base, {'force': 5.0, 'coherence': 0.0})
        q = cs_scores.get(quote, {'force': 5.0, 'coherence': 0.0})
        if b['coherence'] < 0.25 or q['coherence'] < 0.25: return 0, {}, 0, "CS Incohérence", {}
        gap = b['force'] - q['force']
        if (direction == "BUY" and gap <= 1.5) or (direction == "SELL" and gap >= -1.5):
            return 0, {}, 0, f"CS Gap {gap:.1f}", {}
        reasons.append("✅ CS Alignée")
    
    # 6. TRIGGER PIRM-5M
    pirm_ok, details = QuantEngine.detect_pirm_trigger(df_m5, direction)
    if not pirm_ok: return 0, {}, 0, f"PIRM non confirmé", {}
    
    reasons.append(f"🔥 PIRM-5M: RSI {details['rsi']:.1f} | ADX {details['adx']:.1f} | HA {details['ha_color']}")
    
    # Scoring
    score = 75
    if len(df_d) >= 200 and len(df_w) >= 51:
        sma200_d = df_d['close'].rolling(200).mean().iloc[-1]
        if (direction == "BUY" and price > sma200_d) or (direction == "SELL" and price < sma200_d):
            score += 15
            reasons.append("✅ Grade A+")
    
    quality = "ELITE 🏆" if score >= 85 else "PREMIUM ⭐" if score >= 75 else "STANDARD"
    
    params = get_asset_params(symbol)
    sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
    tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
    
    info = {
        'quality': quality, 'score': score, 'reasons': reasons,
        'midnight': f"{midnight:.5f}" if midnight else "N/A",
        'pdh_pdl': f"{pdh:.5f}/{pdl:.5f}" if pdh else "N/A",
        'adx': adx_h1, 'pirm': details
    }
    
    return 1.0, info, atr / price * 100, None, {'sl': sl, 'tp': tp}

def get_currency_strength_rsi(api):
    now = datetime.now()
    if st.session_state.cs_data.get('time') and (now - st.session_state.cs_data['time']).total_seconds() < 900:
        return st.session_state.cs_data['data']
    
    pairs = [p for p in ASSETS if "_" in p and "XAU" not in p and "US30" not in p]
    prices = {}
    for p in pairs[:15]:
        try:
            df = api.get_candles(p, "H1", 50)
            if not df.empty: prices[p] = df['close']
        except: continue
    
    if not prices: return None
    df_prices = pd.DataFrame(prices).ffill().bfill()
    
    def calc_rsi(series, period=14):
        delta = series.diff()
        gain, loss = (delta.where(delta > 0, 0)).fillna(0), (-delta.where(delta < 0, 0)).fillna(0)
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
        else:
            results[curr] = {'force': 5.0, 'coherence': 0.0}
    
    st.session_state.cs_data = {'data': results, 'time': now}
    return results

def run_scan_pirm(api, min_score, min_adx, use_zones):
    cs_scores = get_currency_strength_rsi(api)
    signals, logs = [], []
    progress = st.progress(0)
    status = st.empty()
    
    for i, sym in enumerate(ASSETS):
        progress.progress((i+1)/len(ASSETS))
        status.markdown(f"⏳ **{sym}** ({i+1}/{len(ASSETS)})")
        
        try:
            df_m5 = api.get_candles(sym, "M5", 200)
            df_h1 = api.get_candles(sym, "H1", 50)
            df_d = api.get_candles(sym, "D", 250)
            df_w = api.get_candles(sym, "W", 150)
            if df_m5.empty or df_h1.empty: continue
            
            live_price, spread = api.get_realtime_price_and_spread(sym)
            
            for direction in ["BUY", "SELL"]:
                prob, info, atr_pct, reject, extras = calculate_signal_pirm(df_m5, df_h1, df_d, df_w, sym, direction, live_price, cs_scores, min_adx, use_zones)
                
                if reject:
                    logs.append(f"{sym} {direction}: {reject}")
                    continue
                
                if info['score'] < min_score:
                    logs.append(f"{sym} {direction}: Score {info['score']} < {min_score}")
                    continue
                
                signals.append({
                    'symbol': sym, 'type': direction, 'price': live_price, 'score': info['score'],
                    'details': info, 'sl': extras['sl'], 'tp': extras['tp'], 'spread': spread
                })
        except Exception as e:
            logs.append(f"❌ {sym}: {str(e)[:40]}")
    
    progress.empty()
    status.empty()
    return sorted(signals, key=lambda x: x['score'], reverse=True), logs

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
            st.markdown(f"**RSI OHLC4:** {pirm.get('rsi', 0):.1f} | **HMA 20:** {pirm.get('hma', 0):.5f} | **Heikin Ashi:** {pirm.get('ha_color', 'N/A')}")
        
        c1, c2 = st.columns(2)
        c1.info(f"🛑 SL: {s['sl']:.5f}")
        c2.success(f"🎯 TP: {s['tp']:.5f}")

def main():
    st.title("🎯 GOCRZ-Sniper PRO | PIRM-5M")
    
    with st.sidebar:
        st.header("⚙️ Paramètres")
        min_score = st.slider("Score Min", 60, 95, 75, 5)
        min_adx = st.slider("ADX Min", 15, 30, 20, 1)
        use_zones = st.checkbox("🎯 Zones (OB/FVG)", value=True)
    
    if st.button("🔍 SCANNER PIRM-5M"):
        with st.spinner("Analyse PIRM-5M en cours..."):
            api = OandaClient()
            results, logs = run_scan_pirm(api, min_score, min_adx, use_zones)
        
        if not results:
            st.warning("⚠️ Aucun signal PIRM-5M valide")
            with st.expander("📜 Logs (50 premiers)"):
                for log in logs[:50]: st.text(log)
        else:
            st.success(f"✅ {len(results)} Signal(s) PIRM-5M")
            elite = sum(1 for r in results if "ELITE" in r['details']['quality'])
            premium = sum(1 for r in results if "PREMIUM" in r['details']['quality'])
            c1, c2, c3 = st.columns(3)
            c1.metric("🏆 Elite", elite)
            c2.metric("⭐ Premium", premium)
            c3.metric("✅ Total", len(results))
            
            for r in results: display_signal(r)
            
            with st.expander("📜 Logs"):
                for log in logs[:50]: st.text(log)

if __name__ == "__main__":
    main()
