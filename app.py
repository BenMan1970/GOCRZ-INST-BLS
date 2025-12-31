# ============================================================
# BLUESTAR ULTIMATE V4 — INSTITUTIONAL FINAL (OPTIMIZED)
# ============================================================

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import logging
from datetime import datetime
from scipy import stats
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONFIGURATION & UI
# ============================================================

st.set_page_config(
    page_title="Bluestar Ultimate V4 [Speed]",
    layout="centered",
    page_icon="⚡"
)

logging.basicConfig(level=logging.INFO)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700;900&display=swap');
* { font-family: 'Roboto', sans-serif; }
.stApp {
    background-color: #0f1117;
    background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%);
}
.metric-card {
    background-color: #1e293b;
    border-radius: 10px;
    padding: 15px;
    border: 1px solid #334155;
    margin-bottom: 10px;
}
h1 {
    background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 900; font-size: 2.5em; text-align: center;
}
</style>
""", unsafe_allow_html=True)

# ============================================================
# DATA ENGINE (CACHED & OPTIMIZED)
# ============================================================

# On initialise le client une seule fois au niveau global si possible, 
# ou on recrée léger à chaque appel pour éviter les conflits de threads.
TOKEN = st.secrets["OANDA_ACCESS_TOKEN"]
ENV = st.secrets.get("OANDA_ENVIRONMENT", "practice")

@st.cache_data(ttl=60, show_spinner=False)
def fetch_candles_optimized(instrument, granularity, count):
    """Récupère les bougies avec cache natif Streamlit (60s)"""
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
                    "time": c["time"], # On garde en string pour perf, converti si besoin
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": int(c["volume"])
                })
        
        df = pd.DataFrame(data)
        return df
    except Exception as e:
        logging.error(f"Error fetching {instrument} {granularity}: {e}")
        return pd.DataFrame()

# ============================================================
# ASSETS
# ============================================================

ASSETS = [
    "EUR_USD","GBP_USD","USD_JPY","USD_CHF","AUD_USD","USD_CAD","NZD_USD",
    "EUR_GBP","EUR_JPY","EUR_CHF","EUR_CAD","EUR_AUD","EUR_NZD",
    "GBP_JPY","GBP_CHF","GBP_CAD","GBP_AUD","GBP_NZD",
    "AUD_JPY","AUD_CAD","AUD_CHF","AUD_NZD",
    "CAD_JPY","CAD_CHF","NZD_JPY","NZD_CAD","NZD_CHF","CHF_JPY",
    "XAU_USD","US30_USD"
]

# ============================================================
# CALCULATION ENGINES
# ============================================================

class QuantEngine:
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
    def rsi(df, period=7):
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        rs = gain.ewm(alpha=1/period, adjust=False).mean() / \
             loss.ewm(alpha=1/period, adjust=False).mean()
        return 100 - (100 / (1 + rs))

    @staticmethod
    def zscore_structure(df, lookback=20):
        if len(df) < lookback: return 0
        # Optimisation : calcul manuel plus rapide que scipy pour un simple zscore sur array
        vals = df["close"].iloc[-lookback:].values
        z = (vals[-1] - np.mean(vals)) / np.std(vals)
        if z > 1.5: return 1
        if z < -1.5: return -1
        return 0

class InstitutionalEngine:
    @staticmethod
    def get_regime_and_obv(df_h4, df_m5):
        # ADX Calculation (H4)
        high, low, close = df_h4["high"], df_h4["low"], df_h4["close"]
        tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
        atr = tr.ewm(span=14, adjust=False).mean()
        up, down = high.diff(), -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0)
        minus_dm = np.where((down > up) & (down > 0), down, 0)
        plus_di = 100 * pd.Series(plus_dm).ewm(span=14, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus_dm).ewm(span=14, adjust=False).mean() / atr
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx_val = dx.ewm(span=14, adjust=False).mean().iloc[-1]

        # Regime Logic
        regime_mult = 0.0
        if adx_val >= 22: regime_mult = 1.0
        elif adx_val >= 15: regime_mult = 0.7
        # else Range (0.0)
        
        # OBV Calculation (M5)
        change = df_m5["close"].diff()
        direction = np.where(change > 0, df_m5["volume"], np.where(change < 0, -df_m5["volume"], 0))
        obv = pd.Series(direction).cumsum()
        ma_obv = obv.rolling(20).mean()
        
        obv_mult = 1.0
        if not ma_obv.dropna().empty:
            obv_mult = 1.15 if obv.iloc[-1] > ma_obv.iloc[-1] else 0.85

        return regime_mult, obv_mult

# ============================================================
# CORE LOGIC (SINGLE ASSET PROCESSING)
# ============================================================

def process_asset(sym, min_conf):
    """Traite un seul actif. Fonction conçue pour le multi-threading."""
    try:
        # 1. Fetch Data (Parallel & Cached)
        # Note: D1 est peu utilisé dans ton calcul final, je l'ai retiré pour gagner 33% de vitesse
        # Si tu en as besoin, décommente la ligne D1.
        df_m5 = fetch_candles_optimized(sym, "M5", 150)
        df_h4 = fetch_candles_optimized(sym, "H4", 100)
        # df_d = fetch_candles_optimized(sym, "D", 100) 

        if df_m5.empty or df_h4.empty:
            return None

        # 2. Fast Filter: RSI Cross
        rsi = QuantEngine.rsi(df_m5)
        rsi_prev, rsi_curr = rsi.iloc[-2], rsi.iloc[-1]
        
        direction = None
        if rsi_prev < 50 <= rsi_curr: direction = "BUY"
        elif rsi_prev > 50 >= rsi_curr: direction = "SELL"
        else: return None # Pas de signal, on sort vite

        # 3. Deep Analysis
        regime_mult, obv_mult = InstitutionalEngine.get_regime_and_obv(df_h4, df_m5)
        if regime_mult == 0: return None # Marché en range, on filtre

        # 4. Probability Computation
        rsi_mom = rsi_curr - rsi_prev
        base_prob = 0.6 + min(abs(rsi_mom) / 10, 0.2)
        
        z = QuantEngine.zscore_structure(df_h4)
        z_conf = 1.15 if (direction == "BUY" and z > 0) or (direction == "SELL" and z < 0) else 0.9
        
        final_prob = base_prob * regime_mult * obv_mult * z_conf
        final_prob = min(final_prob, 0.99)

        if final_prob < min_conf:
            return None

        # 5. Risk Management
        price = df_m5["close"].iloc[-1]
        atr = QuantEngine.atr(df_m5)
        sl = price - atr * 1.5 if direction == "BUY" else price + atr * 1.5
        tp = price + atr * 3.0 if direction == "BUY" else price - atr * 3.0

        return {
            "symbol": sym,
            "type": direction,
            "price": price,
            "prob": final_prob,
            "time": df_m5["time"].iloc[-1],
            "sl": sl,
            "tp": tp,
            "rr": 2.0
        }

    except Exception as e:
        return None

# ============================================================
# MAIN APP
# ============================================================

def main():
    st.title("⚡ BLUESTAR V4 SPEED")
    st.caption("Scanner Institutionnel | Multi-Threaded Engine")

    col1, col2 = st.columns([3, 1])
    with col1:
        min_conf = st.slider("Confiance minimale (%)", 60, 95, 75) / 100
    with col2:
        st.write("")
        st.write("")
        scan_btn = st.button("🚀 SCANNER", use_container_width=True)

    if scan_btn:
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # ThreadPoolExecutor pour paralléliser les requêtes
        # max_workers=10 est un bon équilibre pour ne pas se faire bloquer par OANDA
        with ThreadPoolExecutor(max_workers=10) as executor:
            # Lancement des tâches
            future_to_asset = {executor.submit(process_asset, sym, min_conf): sym for sym in ASSETS}
            
            completed = 0
            for future in as_completed(future_to_asset):
                sym = future_to_asset[future]
                completed += 1
                
                # Mise à jour UI
                perc = int(completed / len(ASSETS) * 100)
                progress_bar.progress(perc)
                status_text.text(f"Analyse de {sym}... ({completed}/{len(ASSETS)})")
                
                res = future.result()
                if res:
                    results.append(res)

        progress_bar.empty()
        status_text.empty()

        if not results:
            st.warning("Aucun signal détecté avec cette configuration.")
        else:
            results = sorted(results, key=lambda x: x["prob"], reverse=True)
            st.success(f"{len(results)} Signaux détectés")
            
            for s in results:
                # Design Card
                color = "#00ff88" if s['type'] == "BUY" else "#ff4b4b"
                emoji = "🟢" if s['type'] == "BUY" else "🔴"
                
                with st.expander(f"{emoji} {s['symbol']}  |  {s['type']}  |  Prob: {int(s['prob']*100)}%"):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Prix Entrée", f"{s['price']:.5f}")
                    c2.metric("Stop Loss", f"{s['sl']:.5f}", delta=f"-{(abs(s['price']-s['sl'])):.5f}", delta_color="inverse")
                    c3.metric("Take Profit", f"{s['tp']:.5f}", delta=f"+{(abs(s['tp']-s['price'])):.5f}")
                    
                    st.caption(f"📅 Signal Time: {s['time']}")

if __name__ == "__main__":
    main()

