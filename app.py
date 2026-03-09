import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf

# ===============================
# BLUESTAR SNIPER V10 ENGINE
# ===============================

class QuantEngine:

    @staticmethod
    def wma(series, period):
        """Moyenne Mobile Pondérée (Weighted Moving Average) nécessaire pour le HMA"""
        weights = np.arange(1, period + 1)
        return series.rolling(period).apply(lambda prices: np.dot(prices, weights) / weights.sum(), raw=True)

    # ===============================
    # ATR WILDER
    # ===============================
    @staticmethod
    def calculate_atr_wilder(df, period=14):
        high = df['high']
        low = df['low']
        close = df['close']

        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        return atr.iloc[-1]

    # ===============================
    # ADX WILDER (Corrigé)
    # ===============================
    @staticmethod
    def adx_wilder(df, period=14):
        high = df['high']
        low = df['low']
        close = df['close']

        up_move = high.diff()
        down_move = -low.diff() # Équivalent à low.shift() - low

        # Conditions exactes de Wilder
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

        plus_dm = pd.Series(plus_dm, index=df.index)
        minus_dm = pd.Series(minus_dm, index=df.index)

        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1/period, adjust=False).mean()

        plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)

        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
        adx = dx.ewm(alpha=1/period, adjust=False).mean()

        return adx.iloc[-1], plus_di.iloc[-1], minus_di.iloc[-1]

    # ===============================
    # HULL MOVING AVERAGE (Corrigé avec WMA)
    # ===============================
    @staticmethod
    def hma(series, period=55):
        half = int(period / 2)
        sqrt = int(np.sqrt(period))

        wma1 = QuantEngine.wma(series, half)
        wma2 = QuantEngine.wma(series, period)

        raw_hma = 2 * wma1 - wma2
        hma = QuantEngine.wma(raw_hma, sqrt)

        return hma

# ===============================
# ICT PD ARRAYS
# ===============================

def pd_arrays_location(price, pdh, pdl):
    dealing_range = pdh - pdl
    equilibrium = pdl + dealing_range * 0.5
    discount_25 = pdl + dealing_range * 0.25
    premium_75 = pdl + dealing_range * 0.75

    if price < discount_25:
        return "DEEP_DISCOUNT"
    elif price < equilibrium:
        return "DISCOUNT"
    elif price < premium_75:
        return "PREMIUM"
    else:
        return "DEEP_PREMIUM"

# ===============================
# SIGNAL ENGINE V10 (Logique de score améliorée)
# ===============================

def calculate_signal_v10(df_m5, df_d):
    reasons =[]
    base_score = 0 # Positif = BUY, Négatif = SELL
    price = df_m5['close'].iloc[-1]

    # ===============================
    # 1. PD ARRAYS (Tendance long terme)
    # ===============================
    pdh = df_d['high'].iloc[-2]
    pdl = df_d['low'].iloc[-2]
    location = pd_arrays_location(price, pdh, pdl)

    if location == "DEEP_DISCOUNT":
        base_score += 30
        reasons.append("🟢 Deep Discount Daily (+30 Haussier)")
    elif location == "DISCOUNT":
        base_score += 15
        reasons.append("🟢 Discount Zone Daily (+15 Haussier)")
    elif location == "PREMIUM":
        base_score -= 15
        reasons.append("🔴 Premium Area Daily (-15 Baissier)")
    elif location == "DEEP_PREMIUM":
        base_score -= 30
        reasons.append("🔴 Deep Premium Daily (-30 Baissier)")

    # ===============================
    # 2. TREND (HMA Court terme)
    # ===============================
    hma = QuantEngine.hma(df_m5['close'], 55)
    
    if price > hma.iloc[-1]:
        base_score += 20
        reasons.append("🟢 Trend M5 Up (Price > HMA) (+20 Haussier)")
    else:
        base_score -= 20
        reasons.append("🔴 Trend M5 Down (Price < HMA) (-20 Baissier)")

    # ---> DÉDUCTION DE LA DIRECTION PRINCIPALE <---
    direction = "BUY" if base_score > 0 else "SELL" if base_score < 0 else "NEUTRAL"
    score = abs(base_score) # On passe en force absolue (0 à 100+)

    # ===============================
    # 3. ATR (Volatilité)
    # ===============================
    atr = QuantEngine.calculate_atr_wilder(df_m5)
    atr_mean = (df_m5['high'] - df_m5['low']).rolling(20).mean().iloc[-1]
    if atr > atr_mean:
        score += 10
        reasons.append("⚡ ATR Expansion détectée (+10 Force)")

    # ===============================
    # 4. ADX (Force de la tendance)
    # ===============================
    adx, plus_di, minus_di = QuantEngine.adx_wilder(df_m5)
    if adx > 25:
        score += 20
        reasons.append(f"🔥 Strong Trend ADX {adx:.1f} (+20 Force)")
    elif adx > 20:
        score += 10
        reasons.append(f"🔥 Moderate Trend ADX {adx:.1f} (+10 Force)")
    else:
        score -= 10
        reasons.append(f"⚠️ Weak Trend ADX {adx:.1f} (-10 Force)")

    # ===============================
    # 5. QUALITY CLASSIFICATION
    # ===============================
    if direction == "NEUTRAL" or score < 40:
        quality = "IGNORE"
    elif score >= 70:
        quality = "A+ SETUP"
    elif score >= 55:
        quality = "A SETUP"
    elif score >= 40:
        quality = "B SETUP"

    return {
        "direction": direction,
        "score": score,
        "quality": quality,
        "location": location,
        "adx": round(adx, 2),
        "atr": round(atr, 5),
        "reasons": reasons
    }


# ===============================
# INTERFACE STREAMLIT
# ===============================

def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    
    st.title("🎯 BLUESTAR SNIPER V10 - QUANT ENGINE")
    st.markdown("Scanner institutionnel basé sur ICT (PD Arrays) + Hull Moving Average + ADX/ATR.")

    col1, col2 = st.columns([1, 3])
    with col1:
        st.subheader("Configuration")
        ticker = st.text_input("Symbole Yahoo Finance", "BTC-USD")
        st.caption("Exemples : EURUSD=X (Forex), BTC-USD (Crypto), AAPL (Actions)")
        run_scan = st.button("Lancer le Scan 🚀", use_container_width=True)

    if run_scan:
        with st.spinner(f"Récupération des données pour {ticker}..."):
            try:
                # Récupération des données via yfinance
                t = yf.Ticker(ticker)
                
                # Récupère l'historique sur 5 jours
                df_d = t.history(period="10d", interval="1d")
                df_m5 = t.history(period="5d", interval="5m")
                
                # Vérifie si le ticker existe
                if df_d.empty or df_m5.empty:
                    st.error(f"Aucune donnée trouvée pour {ticker}. Vérifiez le symbole.")
                    return

                # Normaliser les noms de colonnes en minuscules
                df_d.columns = [c.lower() for c in df_d.columns]
                df_m5.columns = [c.lower() for c in df_m5.columns]

                # Calcul du signal
                signal = calculate_signal_v10(df_m5, df_d)

                # ===============================
                # AFFICHAGE DES RESULTATS
                # ===============================
                with col2:
                    st.subheader(f"Résultats de l'analyse : {ticker.upper()}")
                    
                    # Indicateurs principaux
                    met1, met2, met3, met4 = st.columns(4)
                    
                    dir_color = "🟢" if signal["direction"] == "BUY" else "🔴" if signal["direction"] == "SELL" else "⚪"
                    met1.metric("Direction", f"{dir_color} {signal['direction']}")
                    met2.metric("Qualité du Setup", signal['quality'])
                    met3.metric("Score de Force", f"{signal['score']} / 100")
                    met4.metric("ADX (Tendance)", signal['adx'])

                    # Design conditionnel selon la qualité
                    if signal['quality'] == "A+ SETUP":
                        st.success("💎 **SETUP EXCELLENT DÉTECTÉ** - Confluences optimales.")
                    elif signal['quality'] == "IGNORE":
                        st.warning("⚠️ **PAS DE SETUP VALIDE** - Conditions de marché médiocres.")
                    else:
                        st.info("✅ **SETUP VALIDE** - Opportunité détectée.")

                    st.markdown("### 📊 Justification du Signal")
                    for r in signal['reasons']:
                        st.write(f"- {r}")
                        
            except Exception as e:
                st.error(f"Une erreur s'est produite lors du calcul : {e}")

if __name__ == "__main__":
    main()
