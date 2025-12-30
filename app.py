# =============================================================================
# BLUESTAR SCANNER - Version corrigée et stable - Décembre 2025
# =============================================================================

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import logging
from datetime import datetime

# =============================================================================
# CONFIGURATION PAGE & STYLE
# =============================================================================

st.set_page_config(
    page_title="Bluestar Scanner",
    layout="wide",
    page_icon="📈",
    initial_sidebar_state="expanded"
)

# Style sombre inspiré des versions précédentes
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap');
    * { font-family: 'Roboto', sans-serif; }
    .stApp {
        background-color: #0f1117;
        background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%);
    }
    .main .block-container { max-width: 1100px; padding-top: 1.8rem; }
    h1 {
        background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 900; font-size: 2.7em; text-align: center;
        margin-bottom: 0.4em;
    }
    .stButton>button {
        width: 100%; border-radius: 8px; height: 3em;
        background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%);
        color: white; font-weight: 600; border: none;
    }
    .metric-container {
        background: rgba(30,41,59,0.65);
        border: 1px solid #334155;
        border-radius: 8px;
        padding: 14px;
        margin: 8px 0;
    }
    .buy-side   { border-left: 5px solid #10b981; }
    .sell-side  { border-left: 5px solid #ef4444; }
    .badge {
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 0.82em;
        font-weight: 600;
        margin-left: 8px;
        color: white;
    }
    .badge-high { background: #10b981; }
    .badge-med  { background: #f59e0b; }
    </style>
""", unsafe_allow_html=True)

logging.basicConfig(level=logging.INFO)

# =============================================================================
# CLIENT OANDA - Version sans problème de hash pour Streamlit cache
# =============================================================================

class OandaClient:
    def __init__(self):
        try:
            self.client = oandapyV20.API(
                access_token=st.secrets["OANDA_ACCESS_TOKEN"],
                environment=st.secrets.get("OANDA_ENVIRONMENT", "practice")
            )
        except Exception as e:
            st.error(f"Erreur de configuration API Oanda : {str(e)}")
            st.stop()

    # Méthode clé : utilisation de _self pour contourner le problème de hash
    @st.cache_data(ttl=120, show_spinner=False)
    def get_candles(_self, instrument: str, granularity: str, count: int = 300) -> pd.DataFrame:
        self = _self  # Liaison explicite avec l'instance
        params = {"count": count, "granularity": granularity, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)
        
        try:
            self.client.request(r)
            data = []
            for candle in r.response.get("candles", []):
                if candle.get("complete"):
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
            return pd.DataFrame(data).set_index("time")
            
        except Exception as e:
            logging.warning(f"Erreur récupération {instrument} {granularity}: {str(e)}")
            return pd.DataFrame()


# Instruments surveillés (liste raisonnable)
INSTRUMENTS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD",
    "NZD_USD", "USD_CHF", "EUR_JPY", "GBP_JPY", "XAU_USD"
]

# =============================================================================
# Indicateurs de base
# =============================================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    tr = np.maximum(df["h"] - df["l"],
                   np.maximum(abs(df["h"] - df["c"].shift()),
                             abs(df["l"] - df["c"].shift())))
    return tr.ewm(span=period, adjust=False).mean().iloc[-1]


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss = -delta.clip(upper=0).ewm(com=period-1, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# =============================================================================
# Logique de détection de signal (version simplifiée mais robuste)
# =============================================================================

def detect_signal(client: OandaClient, instrument: str) -> dict | None:
    try:
        df_m15 = client.get_candles(instrument, "M15", 250)
        df_h4  = client.get_candles(instrument, "H4",  120)

        if len(df_m15) < 100 or len(df_h4) < 60:
            return None

        # Alignement de tendance multi-timeframe
        trend_h4 = df_h4["c"].iloc[-1] > df_h4["c"].ewm(span=34).mean().iloc[-1]

        # Déclencheur RSI M15
        rsi = calculate_rsi(df_m15["c"])
        if len(rsi) < 3:
            return None

        rsi_cross_up   = (rsi.iloc[-2] < 45) and (rsi.iloc[-1] >= 55)
        rsi_cross_down = (rsi.iloc[-2] > 55) and (rsi.iloc[-1] <= 45)

        if not (rsi_cross_up or rsi_cross_down):
            return None

        direction = "BUY" if rsi_cross_up else "SELL"

        # Filtre de cohérence avec la tendance H4
        if (direction == "BUY" and not trend_h4) or (direction == "SELL" and trend_h4):
            return None

        price = float(df_m15["c"].iloc[-1])
        atr   = calculate_atr(df_m15)

        confidence = 0.72 + (abs(rsi.iloc[-1] - 50) / 80) * 0.18
        confidence = min(0.94, confidence)

        return {
            "instrument": instrument,
            "direction": direction,
            "price": round(price, 5),
            "confidence": round(confidence, 3),
            "time": df_m15.index[-1],
            "atr_pct": round(atr / price * 100, 2),
            "sl": round(price - atr * 1.65 if direction == "BUY" else price + atr * 1.65, 5),
            "tp": round(price + atr * 3.5 if direction == "BUY" else price - atr * 3.5, 5)
        }

    except Exception as e:
        logging.error(f"Erreur analyse {instrument}: {str(e)}")
        return None


# =============================================================================
# INTERFACE PRINCIPALE
# =============================================================================

def main():
    st.title("Bluestar Market Scanner")

    client = OandaClient()

    col_config, col_info = st.columns([6, 4])
    with col_config:
        min_confidence = st.slider(
            "Confiance minimale affichée (%)",
            65, 94, 74, 1
        ) / 100.0

    with col_info:
        st.caption(f"Dernière actualisation : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    if st.button("Analyser le marché", type="primary"):
        with st.spinner("Analyse en cours..."):
            signals = []
            progress_bar = st.progress(0)

            for i, instr in enumerate(INSTRUMENTS):
                signal = detect_signal(client, instr)
                if signal and signal["confidence"] >= min_confidence:
                    signals.append(signal)
                progress_bar.progress((i + 1) / len(INSTRUMENTS))

        if not signals:
            st.info("Aucun signal atteignant le seuil de confiance actuel.")
            return

        st.success(f"{len(signals)} signal{'s' if len(signals) > 1 else ''} détecté{'s' if len(signals) > 1 else ''}")

        signals.sort(key=lambda x: -x["confidence"])

        for s in signals:
            side_class = "buy-side" if s["direction"] == "BUY" else "sell-side"
            badge_class = "badge-high" if s["confidence"] >= 0.85 else "badge-med"

            with st.container():
                st.markdown(f"""
                <div class="metric-container {side_class}">
                    <strong>{s['instrument']} {s['direction']}</strong><br>
                    Prix : {s['price']:.5f}  
                    <span class="badge {badge_class}">{int(s['confidence']*100)}%</span>
                      ATR : {s['atr_pct']}%
                </div>
                """, unsafe_allow_html=True)

                cols = st.columns([1,1,1,1])
                cols[0].metric("Stop Loss", f"{s['sl']:.5f}")
                cols[1].metric("Take Profit", f"{s['tp']:.5f}")
                cols[2].metric("R:R", f"{abs((s['tp']-s['price'])/(s['price']-s['sl'])):.1f}")
                cols[3].metric("Heure", s["time"].strftime("%H:%M"))

                st.markdown("---")


if __name__ == "__main__":
    main()
