import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timezone
import time

# ===============================
# BLUESTAR SNIPER V10 ENGINE
# ===============================

class QuantEngine:
    @staticmethod
    def wma(series, period):
        weights = np.arange(1, period + 1)
        return series.rolling(period).apply(lambda prices: np.dot(prices, weights) / weights.sum(), raw=True)

    @staticmethod
    def calculate_atr_wilder(df, period=14):
        tr = pd.concat([df['high'] - df['low'], 
                        abs(df['high'] - df['close'].shift()), 
                        abs(df['low'] - df['close'].shift())], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    @staticmethod
    def adx_wilder(df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        up_move, down_move = high.diff(), -low.diff()
        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=df.index)
        tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        adx = (abs(plus_di - minus_di) / (plus_di + minus_di) * 100).ewm(alpha=1/period, adjust=False).mean()
        return adx.iloc[-1]

    @staticmethod
    def hma(series, period=55):
        half, sqrt = int(period / 2), int(np.sqrt(period))
        wma1, wma2 = QuantEngine.wma(series, half), QuantEngine.wma(series, period)
        return QuantEngine.wma(2 * wma1 - wma2, sqrt)

# ===============================
# LOGIQUE DE SIGNAL
# ===============================

def get_signal(df_m5, df_d):
    score = 0
    price = df_m5['close'].iloc[-1]
    
    # 1. PD ARRAYS (Daily Context)
    pdh, pdl = df_d['high'].iloc[-2], df_d['low'].iloc[-2]
    range_size = pdh - pdl
    if price < pdl + range_size * 0.25: score += 30 # Deep Discount
    elif price < pdl + range_size * 0.5: score += 15 # Discount
    elif price < pdl + range_size * 0.75: score -= 15 # Premium
    else: score -= 30 # Deep Premium

    # 2. HMA TREND
    hma_val = QuantEngine.hma(df_m5['close'], 55).iloc[-1]
    score += 20 if price > hma_val else -20

    # 3. VOLATILITY & ADX
    adx = QuantEngine.adx_wilder(df_m5)
    final_score = abs(score)
    final_score += 20 if adx > 25 else 10 if adx > 20 else -10
    
    direction = "BUY 🔵" if score > 0 else "SELL 🔴"
    
    # Quality Mapping
    if final_score >= 70: quality = "💎 A+ SETUP"
    elif final_score >= 55: quality = "✅ A SETUP"
    elif final_score >= 40: quality = "⚖️ B SETUP"
    else: quality = "❌ IGNORE"

    last_time = df_m5.index[-1]
    freshness = int((datetime.now(timezone.utc) - last_time).total_seconds() / 60)

    return {
        "Direction": direction,
        "Score": min(final_score, 100),
        "Qualité": quality,
        "ADX": round(adx, 1),
        "Fraîcheur": f"{freshness} min"
    }

# ===============================
# DATA FETCHING
# ===============================

def fetch_oanda_data(client, ticker, granularity, count):
    try:
        r = instruments.InstrumentsCandles(instrument=ticker, params={"count": count, "granularity": granularity})
        client.request(r)
        data = [{"time": pd.to_datetime(c["time"]), "high": float(c["mid"]["h"]), 
                 "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])} 
                for c in r.response.get("candles", []) if c["complete"]]
        df = pd.DataFrame(data)
        if not df.empty: df.set_index("time", inplace=True)
        return df
    except: return pd.DataFrame()

# ===============================
# STREAMLIT UI
# ===============================

def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    
    st.title("🎯 BLUESTAR SNIPER V10 - GLOBAL SCANNER")
    st.markdown("### Scanner Institutionnel ICT & Momentum")

    # Configuration API via Secrets
    try:
        token = st.secrets["OANDA_ACCESS_TOKEN"]
        env = st.secrets.get("OANDA_ENV", "practice")
        client = oandapyV20.API(access_token=token, environment=env)
    except:
        st.error("⚠️ Configurer `OANDA_ACCESS_TOKEN` dans les Secrets Streamlit.")
        st.stop()

    # Liste exhaustive : 28 Forex + Gold + Indices
    forex_28 = [
        "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "NZD_USD", "USD_CHF",
        "EUR_GBP", "EUR_JPY", "EUR_AUD", "EUR_CAD", "EUR_NZD", "EUR_CHF",
        "GBP_JPY", "GBP_AUD", "GBP_CAD", "GBP_NZD", "GBP_CHF",
        "AUD_JPY", "AUD_CAD", "AUD_NZD", "AUD_CHF",
        "NZD_JPY", "NZD_CAD", "NZD_CHF",
        "CAD_JPY", "CAD_CHF", "CHF_JPY"
    ]
    indices_commods = ["XAU_USD", "US30_USD", "NAS100_USD", "SPX500_USD", "DE30_EUR"] # DE30_EUR est le DAX sur Oanda
    
    assets = forex_28 + indices_commods

    if st.button("LANCER LE SCAN SUR TOUS LES ACTIFS 🚀", use_container_width=True):
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for idx, ticker in enumerate(assets):
            status_text.text(f"Analyse en cours : {ticker}...")
            progress_bar.progress((idx + 1) / len(assets))
            
            df_d = fetch_oanda_data(client, ticker, "D", 15)
            df_m5 = fetch_oanda_data(client, ticker, "M5", 500)
            
            if not df_d.empty and not df_m5.empty:
                res = get_signal(df_m5, df_d)
                res["Actif"] = ticker
                results.append(res)
            time.sleep(0.05) # Petit délai pour la stabilité

        status_text.empty()

        if results:
            df_final = pd.DataFrame(results)
            df_final = df_final[["Actif", "Direction", "Qualité", "Score", "ADX", "Fraîcheur"]]
            
            # Tri par Score décroissant pour voir les meilleurs setups en haut
            df_final = df_final.sort_values(by="Score", ascending=False)

            # TOP ALERTS A+
            best_setups = df_final[df_final["Qualité"].str.contains("A\+")]
            if not best_setups.empty:
                st.subheader("💎 ALERTS : TOP SETUPS DÉTECTÉS")
                cols = st.columns(min(len(best_setups), 4))
                for i, (_, row) in enumerate(best_setups.head(4).iterrows()):
                    cols[i % 4].metric(row["Actif"], row["Direction"], f"Score: {row['Score']}")
            
            st.subheader("📊 Tableau Récapitulatif")
            
            # Styling
            def style_rows(row):
                if "A+" in row["Qualité"]:
                    return ['background-color: #1e3d24'] * len(row) # Vert foncé pour A+
                elif "IGNORE" in row["Qualité"]:
                    return ['color: #555555'] * len(row) # Gris pour Ignore
                return [''] * len(row)

            st.dataframe(
                df_final.style.apply(style_rows, axis=1)
                .background_gradient(cmap='RdYlGn', subset=['Score'], vmin=0, vmax=100),
                use_container_width=True,
                height=800
            )
            
            st.success(f"Analyse terminée avec succès à {datetime.now().strftime('%H:%M')}")
        else:
            st.error("Erreur : Impossible de récupérer les données. Vérifiez votre Token OANDA.")

if __name__ == "__main__":
    main()

