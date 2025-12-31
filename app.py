# =============================================================================
# BLUESTAR SCANNER - Version Réparée - Janvier 2026
# =============================================================================

import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from oandapyV20 import API
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

# Style CSS amélioré
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap');
    * { font-family: 'Roboto', sans-serif; }
    .stApp {
        background-color: #0f1117;
        background-image: radial-gradient(at 50% 0%, #1f2937 0%, #0f1117 70%);
    }
    .main .block-container { max-width: 1100px; padding-top: 2rem; }
    h1 {
        background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 900; font-size: 2.5em; text-align: center;
        margin-bottom: 1rem;
    }
    div.stButton > button {
        width: 100%; border-radius: 8px; height: 3.5em;
        background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%);
        color: white; font-weight: 600; border: none;
        transition: transform 0.1s;
    }
    div.stButton > button:hover { transform: scale(1.02); }
    
    .metric-box {
        background: rgba(30,41,59,0.7);
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 15px;
        margin-bottom: 10px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
    }
    .buy-border { border-left: 5px solid #10b981; }
    .sell-border { border-left: 5px solid #ef4444; }
    
    .signal-header { font-size: 1.2em; font-weight: bold; color: #e2e8f0; }
    .badge {
        padding: 3px 8px; border-radius: 4px; font-size: 0.8em; font-weight: bold; margin-left: 10px; color: #fff; vertical-align: middle;
    }
    .bg-green { background-color: #10b981; }
    .bg-orange { background-color: #f59e0b; }
    .bg-red { background-color: #ef4444; }
    </style>
""", unsafe_allow_html=True)

logging.basicConfig(level=logging.INFO)

# =============================================================================
# GESTION API OANDA (Optimisé pour Streamlit)
# =============================================================================

@st.cache_resource
def get_oanda_client():
    """Crée une connexion persistante à l'API Oanda."""
    try:
        # Vérification de la présence des secrets
        if "OANDA_ACCESS_TOKEN" not in st.secrets:
            st.error("⚠️ Clé API manquante ! Veuillez configurer .streamlit/secrets.toml")
            st.stop()
            
        token = st.secrets["OANDA_ACCESS_TOKEN"]
        env = st.secrets.get("OANDA_ENVIRONMENT", "practice")
        return API(access_token=token, environment=env)
    except Exception as e:
        st.error(f"Erreur de connexion Oanda : {str(e)}")
        return None

@st.cache_data(ttl=60, show_spinner=False)
def fetch_candles(_client, instrument: str, granularity: str, count: int = 300) -> pd.DataFrame:
    """Récupère les bougies. L'argument _client est ignoré par le hash (underscore)."""
    if _client is None:
        return pd.DataFrame()
        
    params = {"count": count, "granularity": granularity, "price": "M"}
    r = instruments.InstrumentsCandles(instrument=instrument, params=params)
    
    try:
        _client.request(r)
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
        logging.warning(f"Erreur data {instrument} {granularity}: {str(e)}")
        return pd.DataFrame()

# Liste des instruments Forex majeurs
INSTRUMENTS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD",
    "NZD_USD", "USD_CHF", "EUR_JPY", "GBP_JPY", "XAU_USD"
]

# =============================================================================
# INDICATEURS TECHNIQUES
# =============================================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    if df.empty: return 0.0
    h, l, c = df["h"], df["l"], df["c"]
    tr1 = h - l
    tr2 = abs(h - c.shift())
    tr3 = abs(l - c.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean().iloc[-1]

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss = -delta.clip(upper=0).ewm(com=period-1, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

# =============================================================================
# MOTEUR DE DÉTECTION
# =============================================================================

def detect_signal(client, instrument: str) -> dict | None:
    try:
        # Récupération M15 et H4
        df_m15 = fetch_candles(client, instrument, "M15", 200)
        df_h4  = fetch_candles(client, instrument, "H4",  100)

        if len(df_m15) < 50 or len(df_h4) < 50:
            return None

        # 1. Tendance de fond (H4) via EMA 50
        ema_h4 = df_h4["c"].ewm(span=50, adjust=False).mean().iloc[-1]
        price_h4 = df_h4["c"].iloc[-1]
        trend_bullish = price_h4 > ema_h4

        # 2. Signal RSI (M15)
        rsi = calculate_rsi(df_m15["c"])
        if len(rsi) < 15: return None
        
        last_rsi = rsi.iloc[-1]
        prev_rsi = rsi.iloc[-2]

        # Détection croisement zones 30/70
        signal_buy = (prev_rsi < 30 and last_rsi >= 30) or (prev_rsi < 40 and last_rsi >= 40 and trend_bullish)
        signal_sell = (prev_rsi > 70 and last_rsi <= 70) or (prev_rsi > 60 and last_rsi <= 60 and not trend_bullish)

        if not (signal_buy or signal_sell):
            return None

        direction = "BUY" if signal_buy else "SELL"
        
        # Filtre de tendance : On ne prend que dans le sens du H4 pour plus de sécurité
        if direction == "BUY" and not trend_bullish: return None
        if direction == "SELL" and trend_bullish: return None

        # Calculs Stop Loss / Take Profit via ATR
        price = float(df_m15["c"].iloc[-1])
        atr = calculate_atr(df_m15)
        
        # Confiance basée sur la force du mouvement
        confidence = 0.70
        if (direction == "BUY" and last_rsi < 45) or (direction == "SELL" and last_rsi > 55):
            confidence += 0.15 # Meilleur point d'entrée
        
        sl_mult = 1.5
        tp_mult = 3.0
        
        sl = price - (atr * sl_mult) if direction == "BUY" else price + (atr * sl_mult)
        tp = price + (atr * tp_mult) if direction == "BUY" else price - (atr * tp_mult)

        return {
            "instrument": instrument,
            "direction": direction,
            "price": price,
            "confidence": min(0.95, confidence),
            "time": df_m15.index[-1],
            "atr": atr,
            "sl": sl,
            "tp": tp
        }

    except Exception as e:
        # logging.error(f"Erreur analyse {instrument}: {e}") # Décommenter pour debug
        return None

# =============================================================================
# INTERFACE PRINCIPALE
# =============================================================================

def main():
    st.title("⚡ Bluestar Market Scanner")
    st.caption(f"Date système : {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Initialisation Client API
    client = get_oanda_client()
    if not client:
        st.warning("Client Oanda non initialisé. Vérifiez vos secrets.")
        return

    # Barre latérale
    with st.sidebar:
        st.header("Filtres")
        min_conf_input = st.slider("Confiance Minimale (%)", 60, 95, 75, 5)
        min_confidence = min_conf_input / 100.0
        
        st.info("Scanner basé sur une stratégie de suivi de tendance H4 avec entrées M15 sur retournement RSI.")

    # Bouton d'action
    if st.button("Lancer le Scan", type="primary"):
        results_container = st.container()
        
        with st.spinner("Analyse des marchés en cours..."):
            signals = []
            bar = st.progress(0)
            
            for i, pair in enumerate(INSTRUMENTS):
                sig = detect_signal(client, pair)
                if sig and sig["confidence"] >= min_confidence:
                    signals.append(sig)
                bar.progress((i + 1) / len(INSTRUMENTS))
            
            bar.empty()

        # Affichage des résultats
        if not signals:
            st.info("Aucun signal détecté pour le moment avec ces critères.")
        else:
            st.success(f"{len(signals)} opportunités détectées !")
            signals.sort(key=lambda x: x["confidence"], reverse=True)

            for s in signals:
                # Définition des classes CSS
                border_cls = "buy-border" if s["direction"] == "BUY" else "sell-border"
                color_badge = "bg-green" if s["confidence"] > 0.8 else "bg-orange"
                icon = "🟢" if s["direction"] == "BUY" else "🔴"
                
                # HTML personnalisé pour chaque carte
                html_card = f"""
                <div class="metric-box {border_cls}">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span class="signal-header">{icon} {s['instrument']}</span>
                        <span class="badge {color_badge}">Confiance: {int(s['confidence']*100)}%</span>
                    </div>
                    <div style="margin-top:8px; color:#cbd5e1;">
                        Prix: <strong>{s['price']:.5f}</strong>
                    </div>
                </div>
                """
                st.markdown(html_card, unsafe_allow_html=True)

                # Métriques détaillées
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Stop Loss", f"{s['sl']:.5f}")
                c2.metric("Take Profit", f"{s['tp']:.5f}")
                rr = abs(s['tp'] - s['price']) / abs(s['price'] - s['sl'])
                c3.metric("Ratio R:R", f"1:{rr:.1f}")
                c4.metric("Heure Signal", s['time'].strftime("%H:%M"))
                
                st.markdown("---")

if __name__ == "__main__":
    main()

