# =============================================================================
# BLUESTAR IRONCLAD v2026 – Version corrigée et fonctionnelle
# Conservatoire du style visuel sombre / bleu / vert / rouge des versions précédentes
# =============================================================================

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import logging
from datetime import datetime

# =============================================================================
# CONFIGURATION PAGE & STYLE (très proche des versions précédentes)
# =============================================================================

st.set_page_config(
    page_title="Bluestar Ironclad 2026",
    layout="wide",
    page_icon="🛡️",
    initial_sidebar_state="expanded"
)

# Style inspiré des versions précédentes (sombre + gradient bleu + badges)
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap');
    * { font-family: 'Roboto', sans-serif; }
    .stApp {
        background-color: #0f1117;
        background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%);
    }
    .main .block-container { max-width: 1100px; padding-top: 1.5rem; }
    h1 {
        background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 900; font-size: 2.6em; text-align: center;
    }
    .stButton>button {
        background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%);
        color: white; border-radius: 8px; font-weight: 600;
    }
    .metric-box {
        background: rgba(30,41,59,0.6);
        border: 1px solid #334155;
        border-radius: 8px;
        padding: 12px;
        text-align: center;
    }
    .signal-buy  { border-left: 5px solid #10b981; background: rgba(16,185,129,0.08); }
    .signal-sell { border-left: 5px solid #ef4444; background: rgba(239,68,68,0.08); }
    .badge {
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 0.78em;
        font-weight: 600;
        margin: 0 4px;
        color: white;
        display: inline-block;
    }
    .badge-high { background: #10b981; }
    .badge-med  { background: #f59e0b; }
    </style>
""", unsafe_allow_html=True)

logging.basicConfig(level=logging.INFO)

# =============================================================================
# CLIENT OANDA - corrigé
# =============================================================================

class OandaClient:
    def __init__(self):
        try:
            self.client = oandapyV20.API(
                access_token=st.secrets["OANDA_ACCESS_TOKEN"],
                environment=st.secrets.get("OANDA_ENVIRONMENT", "practice")
            )
        except Exception as e:
            st.error(f"Configuration API Oanda invalide : {e}")
            st.stop()

    @st.cache_data(ttl=90, show_spinner=False)
    def get_candles(self, instrument, granularity, count=300):
        params = {"count": count, "granularity": granularity, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)
        try:
            self.client.request(r)
            data = []
            for candle in r.response["candles"]:
                if candle["complete"]:
                    data.append({
                        "time": pd.to_datetime(candle["time"]),
                        "o": float(candle["mid"]["o"]),
                        "h": float(candle["mid"]["h"]),
                        "l": float(candle["mid"]["l"]),
                        "c": float(candle["mid"]["c"]),
                        "v": int(candle["volume"])
                    })
            if not data:
                return pd.DataFrame()
            df = pd.DataFrame(data).set_index("time")
            return df
        except Exception as e:
            logging.warning(f"Erreur récupération {instrument} {granularity}: {str(e)}")
            return pd.DataFrame()


# Liste d'actifs (réduite pour plus de stabilité)
ASSETS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD",
    "NZD_USD", "USD_CHF", "EUR_JPY", "GBP_JPY", "XAU_USD"
]

# =============================================================================
# INDICATEURS DE BASE
# =============================================================================

def atr(df, period=14):
    tr = np.maximum(df["h"] - df["l"],
                    np.maximum(abs(df["h"] - df["c"].shift()),
                               abs(df["l"] - df["c"].shift())))
    return tr.ewm(span=period, adjust=False).mean().iloc[-1]


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss = -delta.clip(upper=0).ewm(com=period-1, min_periods=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def obv_strength(df):
    obv = (np.sign(df["c"].diff()) * df["v"]).fillna(0).cumsum()
    return (obv.iloc[-1] - obv.iloc[-40]) / (obv.iloc[-40:].std() + 1e-9)


# =============================================================================
# LOGIQUE PRINCIPALE DE SIGNAL
# =============================================================================

def evaluate_signal(client, instrument):
    try:
        df_M15 = client.get_candles(instrument, "M15", 250)
        df_H4  = client.get_candles(instrument, "H4",  120)
        df_D1  = client.get_candles(instrument, "D",   300)

        if len(df_M15) < 100 or len(df_H4) < 60 or len(df_D1) < 100:
            return None

        # Alignement multi-timeframe
        trend_D1 = df_D1["c"].iloc[-1] > df_D1["c"].ewm(span=50).mean().iloc[-1]
        trend_H4 = df_H4["c"].iloc[-1] > df_H4["c"].ewm(span=34).mean().iloc[-1]

        if trend_D1 != trend_H4:
            return None

        # Déclencheur RSI + OBV
        rsi15 = rsi(df_M15["c"])
        rsi_cross_up   = (rsi15.iloc[-2] < 45) and (rsi15.iloc[-1] >= 55)
        rsi_cross_down = (rsi15.iloc[-2] > 55) and (rsi15.iloc[-1] <= 45)

        if not (rsi_cross_up or rsi_cross_down):
            return None

        direction = "BUY" if rsi_cross_up else "SELL"
        if direction == "BUY" and not trend_D1:
            return None
        if direction == "SELL" and trend_D1:
            return None

        obv_str = obv_strength(df_M15)
        if abs(obv_str) < 1.4:
            return None

        price = df_M15["c"].iloc[-1]
        atr_val = atr(df_M15)

        conviction = min(0.92, 0.65 + abs(obv_str)*0.12)

        return {
            "instrument": instrument,
            "direction": direction,
            "price": round(price, 5),
            "conviction": round(conviction, 3),
            "time": df_M15.index[-1],
            "atr_pct": round(atr_val / price * 100, 2),
            "sl": round(price - atr_val * 1.7 if direction=="BUY" else price + atr_val * 1.7, 5),
            "tp": round(price + atr_val * 3.6 if direction=="BUY" else price - atr_val * 3.6, 5)
        }

    except Exception as e:
        logging.error(f"Erreur traitement {instrument}: {str(e)}")
        return None


# =============================================================================
# INTERFACE
# =============================================================================

def main():
    st.title("🛡️ Bluestar Ironclad Scanner")

    client = OandaClient()

    col_left, col_right = st.columns([7,3])

    with col_left:
        min_conv = st.slider("Confiance minimale affichée (%)", 65, 92, 74, 1) / 100.0

    with col_right:
        st.caption(f"Dernière mise à jour : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    if st.button("Lancer l'analyse complète", type="primary"):
        with st.spinner("Analyse des instruments en cours..."):
            signals = []
            progress = st.progress(0)
            for i, instr in enumerate(ASSETS):
                signal = evaluate_signal(client, instr)
                if signal and signal["conviction"] >= min_conv:
                    signals.append(signal)
                progress.progress((i+1) / len(ASSETS))

        signals.sort(key=lambda x: -x["conviction"])

        if not signals:
            st.info("Aucun signal de qualité détecté pour le moment.")
            return

        st.success(f"{len(signals)} signal{'s' if len(signals)>1 else ''} détecté{'s' if len(signals)>1 else ''}")

        for s in signals:
            css = "signal-buy" if s["direction"] == "BUY" else "signal-sell"
            badge_class = "badge-high" if s["conviction"] >= 0.85 else "badge-med"

            with st.container():
                st.markdown(f"""
                <div class="metric-box {css}">
                    <strong>{s['instrument']}  {s['direction']}</strong><br>
                    Prix : {s['price']:.5f}  
                    <span class="badge {badge_class}">{int(s['conviction']*100)}%</span>
                      ATR : {s['atr_pct']}%
                </div>
                """, unsafe_allow_html=True)

                cols = st.columns(4)
                cols[0].metric("Stop Loss", f"{s['sl']:.5f}")
                cols[1].metric("Take Profit", f"{s['tp']:.5f}")
                cols[2].metric("Ratio R:R", f"{abs((s['tp']-s['price'])/(s['price']-s['sl'])):.1f}")
                cols[3].metric("Heure", s["time"].strftime("%H:%M"))

                st.markdown("---")


if __name__ == "__main__":
    main()
