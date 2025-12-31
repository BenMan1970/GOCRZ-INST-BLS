# ============================================================
# BLUESTAR V5 — SNIPER EDITION (Trend Pullback Logic)
# ============================================================

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# ============================================================
# CONFIGURATION & UI
# ============================================================

st.set_page_config(
    page_title="Bluestar V5 Sniper",
    layout="wide",
    page_icon="🎯"
)

logging.basicConfig(level=logging.ERROR)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Roboto:wght@300;400;700&display=swap');
* { font-family: 'Roboto', sans-serif; }
.stApp { background-color: #0e1117; }
h1 {
    font-family: 'JetBrains Mono', monospace;
    background: linear-gradient(90deg, #00f260 0%, #0575e6 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 700;
}
.signal-card {
    background-color: #1a1c24;
    border-left: 4px solid #333;
    padding: 15px;
    border-radius: 5px;
    margin-bottom: 10px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.3);
}
.buy-border { border-left-color: #00f260 !important; }
.sell-border { border-left-color: #ff4b4b !important; }
.metric-value { font-family: 'JetBrains Mono', monospace; font-size: 1.1em; }
</style>
""", unsafe_allow_html=True)

# ============================================================
# DATA ENGINE (SMART CACHE)
# ============================================================

TOKEN = st.secrets["OANDA_ACCESS_TOKEN"]
ENV = st.secrets.get("OANDA_ENVIRONMENT", "practice")

@st.cache_data(ttl=55, show_spinner=False)
def fetch_candles(instrument, granularity, count):
    """Récupération optimisée avec gestion d'erreurs silencieuse"""
    try:
        client = oandapyV20.API(access_token=TOKEN, environment=ENV)
        params = {"count": count, "granularity": granularity, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)
        client.request(r)
        
        data = []
        for c in r.response["candles"]:
            if c["complete"]:
                mid = c["mid"]
                data.append({
                    "time": c["time"],
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": int(c["volume"])
                })
        return pd.DataFrame(data)
    except Exception:
        return pd.DataFrame()

# ============================================================
# ALPHA ENGINE (LOGIQUE "PRO")
# ============================================================

class AlphaEngine:
    
    @staticmethod
    def ema(series, span):
        return series.ewm(span=span, adjust=False).mean()

    @staticmethod
    def rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def atr(df, period=14):
        high, low, close = df["high"], df["low"], df["close"]
        tr = pd.concat([
            high - low,
            abs(high - close.shift()),
            abs(low - close.shift())
        ], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean().iloc[-1]

    @staticmethod
    def adx(df, period=14):
        high, low, close = df["high"], df["low"], df["close"]
        tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        
        up, down = high.diff(), -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0)
        minus_dm = np.where((down > up) & (down > 0), down, 0)
        
        plus_di = 100 * pd.Series(plus_dm).ewm(span=period, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus_dm).ewm(span=period, adjust=False).mean() / atr
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        return dx.ewm(span=period, adjust=False).mean().iloc[-1]

    @staticmethod
    def get_h4_trend(df):
        """Détermine la tendance de fond et sa force"""
        if len(df) < 200: return "NEUTRAL", 0
        
        c = df['close']
        ema50 = AlphaEngine.ema(c, 50).iloc[-1]
        ema100 = AlphaEngine.ema(c, 100).iloc[-1]
        ema200 = AlphaEngine.ema(c, 200).iloc[-1]
        adx_val = AlphaEngine.adx(df)

        # Filtre ADX : Si ADX < 20, le marché dort -> Pas de trend
        if adx_val < 20:
            return "RANGE", adx_val

        # Alignement Parfait (Fan)
        if ema50 > ema100 > ema200:
            return "BULLISH", adx_val
        elif ema50 < ema100 < ema200:
            return "BEARISH", adx_val
        
        return "MESSY", adx_val

# ============================================================
# SNIPER SCANNER LOGIC
# ============================================================

def scan_asset(sym):
    # 1. ANALYSE H4 (Filtre Macro)
    # On ne télécharge QUE le H4 d'abord. Si pas de tendance, on arrête.
    df_h4 = fetch_candles(sym, "H4", 250)
    if df_h4.empty: return None

    trend, adx = AlphaEngine.get_h4_trend(df_h4)
    
    # Si le marché est en range ou brouillon, on next.
    if trend in ["RANGE", "MESSY", "NEUTRAL"]:
        return None

    # 2. ANALYSE M5 (Timing Micro)
    # Seulement si le H4 est validé, on va chercher le M5
    df_m5 = fetch_candles(sym, "M5", 100)
    if df_m5.empty: return None

    rsi_series = AlphaEngine.rsi(df_m5['close'], 14)
    current_rsi = rsi_series.iloc[-1]
    
    signal = None
    setup_quality = 0.0

    # --- LOGIQUE D'ENTRÉE ---
    # On cherche un PULLBACK dans la tendance.
    # ACHAT : Tendance H4 Haussière + RSI M5 est "Oversold" (<35) ou remonte
    # VENTE : Tendance H4 Baissière + RSI M5 est "Overbought" (>65) ou redescend

    if trend == "BULLISH":
        # Le prix respire, le RSI est bas, c'est le moment de recharger
        if current_rsi < 40: 
            signal = "BUY"
            # Plus l'ADX est fort et le RSI bas, meilleur est le signal
            setup_quality = (adx / 50) + ((40 - current_rsi) / 20)
            
    elif trend == "BEARISH":
        # Le prix remonte un peu, le RSI est haut, on vend cher
        if current_rsi > 60:
            signal = "SELL"
            setup_quality = (adx / 50) + ((current_rsi - 60) / 20)

    if not signal:
        return None

    # 3. GESTION DU RISQUE
    atr_m5 = AlphaEngine.atr(df_m5)
    if atr_m5 == 0: return None

    price = df_m5["close"].iloc[-1]
    
    # Stop Loss "Large" basé sur la volatilité pour laisser respirer le trade
    sl_dist = atr_m5 * 2.0
    tp_dist = atr_m5 * 3.0 # Ratio 1.5 strict min

    sl = price - sl_dist if signal == "BUY" else price + sl_dist
    tp = price + tp_dist if signal == "BUY" else price - tp_dist
    
    # Normalisation du score (max ~99%)
    prob = min(0.60 + (setup_quality * 0.2), 0.95)

    return {
        "symbol": sym,
        "action": signal,
        "price": price,
        "prob": prob,
        "trend_str": f"{int(adx)}",
        "rsi_m5": int(current_rsi),
        "sl": sl,
        "tp": tp,
        "rr": 1.5
    }

# ============================================================
# LISTE DES ACTIFS
# ============================================================
ASSETS = [
    "EUR_USD","GBP_USD","USD_JPY","USD_CHF","AUD_USD","USD_CAD","NZD_USD",
    "EUR_GBP","EUR_JPY","GBP_JPY","AUD_JPY","XAU_USD","US30_USD", "NAS100_USD"
]

# ============================================================
# MAIN
# ============================================================

def main():
    col1, col2 = st.columns([3, 1])
    with col1:
        st.title("BLUESTAR V5 🎯 SNIPER")
        st.caption("Stratégie: Trend Following H4 + M5 RSI Pullback")
    
    with col2:
        st.write("")
        if st.button("LANCER LE SCANNER", use_container_width=True):
            scan_active = True
        else:
            scan_active = False

    if scan_active:
        results = []
        bar = st.progress(0)
        status = st.empty()
        
        # Parallélisation
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(scan_asset, sym): sym for sym in ASSETS}
            completed = 0
            
            for future in as_completed(futures):
                sym = futures[future]
                completed += 1
                bar.progress(completed / len(ASSETS))
                status.markdown(f"📡 Analyse de **{sym}**...")
                
                try:
                    res = future.result()
                    if res: results.append(res)
                except Exception as e:
                    logging.error(f"Err {sym}: {e}")

        bar.empty()
        status.empty()

        if not results:
            st.info("😴 Le marché est calme. Aucune configuration 'Sniper' détectée.")
        else:
            # Tri par probabilité
            results = sorted(results, key=lambda x: x['prob'], reverse=True)
            
            st.success(f"{len(results)} Opportunités détectées")
            
            for r in results:
                css_class = "buy-border" if r['action'] == "BUY" else "sell-border"
                color = "#00f260" if r['action'] == "BUY" else "#ff4b4b"
                emoji = "🟢 LONG" if r['action'] == "BUY" else "🔴 SHORT"
                
                with st.container():
                    st.markdown(f"""
                    <div class="signal-card {css_class}">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <h2 style="margin:0; color:white;">{r['symbol']}</h2>
                            <h2 style="margin:0; color:{color};">{emoji}</h2>
                        </div>
                        <div style="display:flex; gap:20px; margin-top:10px; color:#aaa; font-size:0.9em;">
                            <span>🎯 Score: <b style="color:white">{int(r['prob']*100)}%</b></span>
                            <span>🌊 Force Trend H4: <b style="color:white">{r['trend_str']}</b></span>
                            <span>📉 RSI M5: <b style="color:white">{r['rsi_m5']}</b></span>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Prix", f"{r['price']:.5f}")
                    c2.metric("SL (Stop)", f"{r['sl']:.5f}", delta_color="inverse")
                    c3.metric("TP (Target)", f"{r['tp']:.5f}")
                    c4.metric("R:R", f"1:{r['rr']}")
                    st.divider()

if __name__ == "__main__":
    main()

