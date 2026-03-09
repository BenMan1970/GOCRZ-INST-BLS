import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timezone

# ===============================
# BLUESTAR SNIPER V10 ENGINE
# ===============================

class QuantEngine:

    @staticmethod
    def wma(series, period):
        """Moyenne Mobile Pondérée pour le HMA"""
        weights = np.arange(1, period + 1)
        return series.rolling(period).apply(lambda prices: np.dot(prices, weights) / weights.sum(), raw=True)

    @staticmethod
    def calculate_atr_wilder(df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    @staticmethod
    def adx_wilder(df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        up_move = high.diff()
        down_move = -low.diff() 
        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=df.index)
        tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
        adx = dx.ewm(alpha=1/period, adjust=False).mean()
        return adx.iloc[-1], plus_di.iloc[-1], minus_di.iloc[-1]

    @staticmethod
    def hma(series, period=55):
        half, sqrt = int(period / 2), int(np.sqrt(period))
        wma1, wma2 = QuantEngine.wma(series, half), QuantEngine.wma(series, period)
        return QuantEngine.wma(2 * wma1 - wma2, sqrt)

# ===============================
# LOGIQUE MÉTIER (ICT & SIGNALS)
# ===============================

def pd_arrays_location(price, pdh, pdl):
    dealing_range = pdh - pdl
    if price < pdl + dealing_range * 0.25: return "DEEP_DISCOUNT"
    elif price < pdl + dealing_range * 0.5: return "DISCOUNT"
    elif price < pdl + dealing_range * 0.75: return "PREMIUM"
    else: return "DEEP_PREMIUM"

def calculate_signal_v10(df_m5, df_d):
    reasons = []
    base_score = 0 
    price = df_m5['close'].iloc[-1]
    last_time = df_m5.index[-1]

    # 1. PD ARRAYS
    pdh, pdl = df_d['high'].iloc[-2], df_d['low'].iloc[-2]
    location = pd_arrays_location(price, pdh, pdl)
    loc_map = {"DEEP_DISCOUNT": (30, "🟢 Deep Discount"), "DISCOUNT": (15, "🟢 Discount"), 
               "PREMIUM": (-15, "🔴 Premium"), "DEEP_PREMIUM": (-30, "🔴 Deep Premium")}
    pts, label = loc_map[location]
    base_score += pts
    reasons.append(f"{label} Daily ({pts:+} pts)")

    # 2. TREND HMA
    hma_val = QuantEngine.hma(df_m5['close'], 55).iloc[-1]
    trend_pts = 20 if price > hma_val else -20
    base_score += trend_pts
    reasons.append(f"{'🟢' if trend_pts > 0 else '🔴'} Trend M5 ({trend_pts:+} pts)")

    # 3. ATR & ADX
    direction = "BUY" if base_score > 0 else "SELL" if base_score < 0 else "NEUTRAL"
    score = abs(base_score)
    atr = QuantEngine.calculate_atr_wilder(df_m5)
    if atr > (df_m5['high'] - df_m5['low']).rolling(20).mean().iloc[-1]:
        score += 10
        reasons.append("⚡ ATR Expansion (+10 pts)")

    adx, _, _ = QuantEngine.adx_wilder(df_m5)
    adx_pts = 20 if adx > 25 else 10 if adx > 20 else -10
    score += adx_pts
    reasons.append(f"{'🔥' if adx_pts > 0 else '⚠️'} ADX {adx:.1f} ({adx_pts:+} pts)")

    # 4. QUALITY
    quality = "IGNORE"
    if score >= 70: quality = "A+ SETUP"
    elif score >= 55: quality = "A SETUP"
    elif score >= 40: quality = "B SETUP"

    # 5. FRESHNESS
    now = datetime.now(timezone.utc)
    freshness_minutes = int((now - last_time).total_seconds() / 60)

    return {
        "direction": direction, "score": min(score, 100), "quality": quality,
        "location": location, "adx": round(adx, 2), "atr": round(atr, 5),
        "reasons": reasons, "freshness": freshness_minutes
    }

# ===============================
# OANDA DATA FETCH
# ===============================

def fetch_oanda_data(client, instrument, granularity, count=500):
    params = {"count": count, "granularity": granularity}
    r = instruments.InstrumentsCandles(instrument=instrument, params=params)
    client.request(r)
    data = []
    for c in r.response.get("candles", []):
        if c["complete"]:
            data.append({"time": pd.to_datetime(c["time"]), "open": float(c["mid"]["o"]),
                         "high": float(c["mid"]["h"]), "low": float(c["mid"]["l"]),
                         "close": float(c["mid"]["c"]), "volume": float(c["volume"])})
    df = pd.DataFrame(data)
    if not df.empty: df.set_index("time", inplace=True)
    return df

# ===============================
# INTERFACE STREAMLIT
# ===============================

def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10")

    try:
        OANDA_TOKEN = st.secrets["OANDA_ACCESS_TOKEN"]
        OANDA_ENV = st.secrets.get("OANDA_ENV", "practice") 
    except KeyError:
        st.error("⚠️ Secret `OANDA_ACCESS_TOKEN` manquant.")
        st.stop()

    client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)

    with st.sidebar:
        st.header("⚙️ Paramètres")
        asset_list = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "XAU_USD", "SPX500_USD", "NAS100_USD", "US30_USD", "WTICO_USD", "BTC_USD"]
        ticker_select = st.selectbox("Sélectionner un actif", asset_list)
        ticker_custom = st.text_input("Ou taper un symbole", "")
        ticker = (ticker_custom if ticker_custom else ticker_select).upper().replace("/", "_")
        run_scan = st.button("Lancer l'Analyse 🚀", use_container_width=True)

    if run_scan:
        with st.spinner(f"Analyse de {ticker}..."):
            try:
                df_d = fetch_oanda_data(client, ticker, "D", 15)
                df_m5 = fetch_oanda_data(client, ticker, "M5", 500)
                
                if df_d.empty or df_m5.empty:
                    st.error("Données indisponibles.")
                    return

                res = calculate_signal_v10(df_m5, df_d)

                # AFFICHAGE
                c1, c2, c3, c4 = st.columns(4)
                
                # Couleur de direction
                d_icon = "🟢" if res["direction"] == "BUY" else "🔴" if res["direction"] == "SELL" else "⚪"
                c1.metric("Direction", f"{d_icon} {res['direction']}")
                
                # Couleur de qualité
                q_color = {"A+ SETUP": "blue", "A SETUP": "green", "B SETUP": "orange", "IGNORE": "gray"}
                c2.markdown(f"**Qualité** : :{q_color[res['quality']]}[{res['quality']}]")
                
                c3.metric("Score Force", f"{res['score']}/100")
                
                # Fraîcheur
                f_color = "green" if res['freshness'] < 10 else "orange" if res['freshness'] < 30 else "red"
                c4.markdown(f"**Fraîcheur** : :{f_color}[{res['freshness']} min]")

                st.divider()

                col_left, col_right = st.columns([1, 1])
                with col_left:
                    st.subheader("📊 Confluences")
                    for r in res['reasons']: st.write(f"- {r}")
                
                with col_right:
                    st.subheader("💡 Verdict")
                    if res['quality'] == "A+ SETUP":
                        st.balloons()
                        st.success(f"**SIGNAL FORT DETECTÉ** sur {ticker}. La confluence ICT + Momentum est optimale.")
                    elif res['quality'] == "IGNORE":
                        st.warning("Marché trop incertain. Attendre une meilleure structure.")
                    else:
                        st.info(f"Opportunité de type {res['quality']}. Vérifier le calendrier économique.")

            except Exception as e:
                st.error(f"Erreur : {e}")

if __name__ == "__main__":
    main()

