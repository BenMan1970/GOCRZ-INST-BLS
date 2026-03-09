import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime
import pytz
import time

# ================================================================
#  BLUESTAR SNIPER V10  —  ICT SIGNAL ENGINE
#
#  LOGIQUE :
#  1. Le SIGNAL est donné par le FLIP de couleur de la HMA 20 M15
#     → Flip VERT  = signal potentiel LONG
#     → Flip ROUGE = signal potentiel SHORT
#     → Fraîcheur : signal valide seulement dans les 30 dernières min (2 bougies M15)
#
#  2. CONFIRMATION (les 3 doivent concorder avec le signal HMA) :
#     → Biais Daily BULLISH (si flip vert) / BEARISH (si flip rouge)
#     → Zone DISCOUNT + proche PDL (si flip vert) / PREMIUM + proche PDH (si flip rouge)
#     → Prix dans un FVG M15 dans le même sens
#
#  3. BONUS MOMENTUM (LONG uniquement) :
#     → ATR M15 actif + ADX > 20 avec +DI > -DI
# ================================================================


# ----------------------------------------------------------------
#  QUANT ENGINE
# ----------------------------------------------------------------
class QuantEngine:

    @staticmethod
    def wma(series, period):
        weights = np.arange(1, period + 1)
        return series.rolling(period).apply(
            lambda p: np.dot(p, weights) / weights.sum(), raw=True
        )

    @staticmethod
    def hma(series, period=20):
        """HMA avec lissage EMA-5 final (style PineScript)."""
        half   = int(period / 2)
        sqrt_p = int(np.sqrt(period))
        raw    = 2 * QuantEngine.wma(series, half) - QuantEngine.wma(series, period)
        if raw.isna().all():
            return pd.Series(np.nan, index=series.index)
        return QuantEngine.wma(raw, sqrt_p).ewm(span=5, adjust=False).mean()

    @staticmethod
    def atr(df, period=14):
        c  = df['close'].shift(1)
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - c).abs(),
            (df['low']  - c).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False).mean()

    @staticmethod
    def adx(df, period=14):
        pdm = df['high'].diff()
        mdm = -df['low'].diff()
        pdm = pdm.where((pdm > mdm) & (pdm > 0), 0.0)
        mdm = mdm.where((mdm > pdm) & (mdm > 0), 0.0)
        atr = QuantEngine.atr(df, period)
        pdi = 100 * pdm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
        mdi = 100 * mdm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
        dx  = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
        return dx.ewm(alpha=1/period, adjust=False).mean(), pdi, mdi


# ----------------------------------------------------------------
#  BIAIS DAILY  (EMA21 / EMA50 sur Daily)
# ----------------------------------------------------------------
def get_daily_bias(df_d):
    if len(df_d) < 55:
        return "NEUTRAL"
    c   = df_d['close']
    e21 = c.ewm(span=21, adjust=False).mean().iloc[-1]
    e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
    cur = c.iloc[-1]
    if cur > e21 > e50:  return "BULLISH"
    if cur < e21 < e50:  return "BEARISH"
    return "NEUTRAL"


# ----------------------------------------------------------------
#  FRAÎCHEUR DU FLIP HMA
#
#  On remonte l'historique HMA pour trouver le dernier flip
#  et on retourne :
#   - flip_type   : "BULL" | "BEAR" | None
#   - candles_ago : combien de bougies M15 depuis le flip
#   - minutes_ago : candles_ago × 15
#
#  Signal frais = flip il y a ≤ 2 bougies (≤ 30 min)
# ----------------------------------------------------------------
def find_last_hma_flip(hma_series, max_lookback=10):
    """
    Parcourt les dernières bougies pour trouver le flip le plus récent.
    Retourne (flip_type, candles_ago) ou (None, None) si pas de flip récent.
    """
    colors = []
    for i in range(len(hma_series) - 1, max(len(hma_series) - max_lookback - 2, 0), -1):
        v_curr = hma_series.iloc[i]
        v_prev = hma_series.iloc[i - 1]
        if pd.isna(v_curr) or pd.isna(v_prev):
            continue
        color = "GREEN" if v_curr > v_prev else "RED"
        colors.append((i, color))

    # Cherche le premier changement de couleur en remontant
    for j in range(len(colors) - 1):
        idx_curr, col_curr = colors[j]
        idx_prev, col_prev = colors[j + 1]

        if col_curr != col_prev:
            # flip trouvé à la bougie idx_curr
            candles_ago = (len(hma_series) - 1) - idx_curr
            flip_type   = "BULL" if col_curr == "GREEN" else "BEAR"
            return flip_type, candles_ago

    return None, None


# ----------------------------------------------------------------
#  DETECTION FVG M15
#  FVG Bullish : low[i] > high[i-2]  → gap vert
#  FVG Bearish : high[i] < low[i-2]  → gap rouge
#  Retourne si le prix est dans un FVG du bon sens
# ----------------------------------------------------------------
def detect_fvg(df, price, lookback=80):
    sub = df.iloc[-(lookback + 3):-1]

    bull_fvgs = []
    bear_fvgs = []

    for i in range(2, len(sub)):
        lo = sub['high'].iloc[i - 2]
        hi = sub['low'].iloc[i]
        if hi > lo:
            bull_fvgs.append((lo, hi))

        lo2 = sub['high'].iloc[i]
        hi2 = sub['low'].iloc[i - 2]
        if hi2 > lo2:
            bear_fvgs.append((lo2, hi2))

    bull_fvgs.sort(key=lambda x: abs(price - (x[0] + x[1]) / 2))
    bear_fvgs.sort(key=lambda x: abs(price - (x[0] + x[1]) / 2))

    in_bull = any(lo <= price <= hi for lo, hi in bull_fvgs)
    in_bear = any(lo <= price <= hi for lo, hi in bear_fvgs)

    return in_bull, in_bear, bull_fvgs[0] if bull_fvgs else None, bear_fvgs[0] if bear_fvgs else None


# ----------------------------------------------------------------
#  ANALYSE PRINCIPALE
# ----------------------------------------------------------------
def analyze_asset(client, ticker):
    try:
        df_d   = fetch_oanda_data(client, ticker, "D",   100)
        df_m15 = fetch_oanda_data(client, ticker, "M15", 400)

        if df_d.empty or df_m15.empty:
            return None

        price = df_m15['close'].iloc[-1]

        # ── BIAIS DAILY ───────────────────────────────────────────
        bias = get_daily_bias(df_d)

        # ── PDH / PDL (jour précédent) ────────────────────────────
        pdh = df_d['high'].iloc[-2]
        pdl = df_d['low'].iloc[-2]

        # ── MIDNIGHT OPEN (00:00 NY) ──────────────────────────────
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        today_ny = datetime.now(ny_tz).date()

        mask   = (
            (df_m15.index.date   == today_ny) &
            (df_m15.index.hour   == 0) &
            (df_m15.index.minute == 0)
        )
        mid_c  = df_m15[mask]
        m_open = mid_c['open'].iloc[0] if not mid_c.empty else df_m15['open'].iloc[0]

        # ── ZONE D'INTÉRÊT ────────────────────────────────────────
        atr_d     = QuantEngine.atr(df_d, 14).iloc[-1]
        below_mid = price < m_open
        above_mid = price > m_open
        near_pdl  = price <= (pdl + atr_d)
        near_pdh  = price >= (pdh - atr_d)

        zone_discount = below_mid and near_pdl
        zone_premium  = above_mid and near_pdh

        zone_label = (
            "📉 DISCOUNT" if zone_discount else
            "📈 PREMIUM"  if zone_premium  else
            "〰️ NEUTRE"
        )

        # ── FVG M15 ───────────────────────────────────────────────
        in_bull_fvg, in_bear_fvg, nb_fvg, nr_fvg = detect_fvg(df_m15, price, lookback=80)

        # ── HMA 20 — TROUVER LE DERNIER FLIP ─────────────────────
        hma = QuantEngine.hma(df_m15['close'], 20)

        if hma.isna().iloc[-5:].any():
            return None

        flip_type, candles_ago = find_last_hma_flip(hma, max_lookback=10)

        # Signal frais = flip dans les 2 dernières bougies (≤ 30 min)
        FRESHNESS_LIMIT = 2
        signal_fresh    = (flip_type is not None) and (candles_ago <= FRESHNESS_LIMIT)

        if candles_ago is not None:
            minutes_ago   = candles_ago * 15
            freshness_str = f"⚡ {minutes_ago} min" if signal_fresh else f"⏳ {minutes_ago} min"
        else:
            freshness_str = "—"

        hma_signal = flip_type   # "BULL" | "BEAR" | None
        hma_color  = "🟢 VERT"  if (hma.iloc[-1] > hma.iloc[-2]) else "🔴 ROUGE"

        # ── ATR + ADX M15 ─────────────────────────────────────────
        atr_m15    = QuantEngine.atr(df_m15, 14)
        atr_val    = round(atr_m15.iloc[-1], 5)
        atr_active = atr_val >= atr_m15.iloc[-50:].mean() * 0.5

        adx_s, pdi_s, mdi_s = QuantEngine.adx(df_m15, 14)
        adx_val  = round(adx_s.iloc[-1], 1)
        pdi_val  = round(pdi_s.iloc[-1], 1)
        mdi_val  = round(mdi_s.iloc[-1], 1)
        adx_bull = adx_val > 20 and pdi_val > mdi_val
        adx_bear = adx_val > 20 and mdi_val > pdi_val

        # ── VALIDATION A+ SETUP ───────────────────────────────────
        #
        #  Le SIGNAL = HMA flip frais (≤ 30 min)
        #  Les CONFIRMATIONS doivent concorder avec ce signal :
        #
        #  LONG  : flip BULL + biais BULLISH + zone DISCOUNT + FVG vert + momentum
        #  SHORT : flip BEAR + biais BEARISH + zone PREMIUM  + FVG rouge
        #
        setup_valid   = False
        signal_type   = "—"
        confirmations = []
        missing       = []

        if signal_fresh and hma_signal == "BULL":
            signal_type = "🟢 LONG"
            # Vérif confirmations
            ok_bias = bias == "BULLISH"
            ok_zone = zone_discount
            ok_fvg  = in_bull_fvg
            ok_atr  = atr_active
            ok_adx  = adx_bull

            if ok_bias:  confirmations.append("Biais ✅")
            else:        missing.append("Biais ❌")
            if ok_zone:  confirmations.append("Zone ✅")
            else:        missing.append("Zone ❌")
            if ok_fvg:   confirmations.append("FVG ✅")
            else:        missing.append("FVG ❌")
            if ok_atr:   confirmations.append("ATR ✅")
            else:        missing.append("ATR ❌")
            if ok_adx:   confirmations.append("ADX ✅")
            else:        missing.append("ADX ❌")

            if ok_bias and ok_zone and ok_fvg and ok_atr and ok_adx:
                setup_valid = True

        elif signal_fresh and hma_signal == "BEAR":
            signal_type = "🔴 SHORT"
            ok_bias = bias == "BEARISH"
            ok_zone = zone_premium
            ok_fvg  = in_bear_fvg

            if ok_bias:  confirmations.append("Biais ✅")
            else:        missing.append("Biais ❌")
            if ok_zone:  confirmations.append("Zone ✅")
            else:        missing.append("Zone ❌")
            if ok_fvg:   confirmations.append("FVG ✅")
            else:        missing.append("FVG ❌")

            if ok_bias and ok_zone and ok_fvg:
                setup_valid = True

        elif not signal_fresh and flip_type == "BULL":
            signal_type = "🟢 LONG (expiré)"
        elif not signal_fresh and flip_type == "BEAR":
            signal_type = "🔴 SHORT (expiré)"

        quality = "💎 A+ SETUP" if setup_valid else "IGNORE"

        # Résumé confirmations
        confirm_str = " | ".join(confirmations) if confirmations else "—"
        missing_str = " | ".join(missing)        if missing       else "—"

        return {
            "Actif":         ticker,
            "Signal HMA":    signal_type,
            "Fraîcheur":     freshness_str,
            "Biais Daily":   bias,
            "Zone":          zone_label,
            "FVG M15":       ("✅ Dans FVG 🟢" if in_bull_fvg else
                              "✅ Dans FVG 🔴" if in_bear_fvg else
                              "〰️ proche 🟢"   if nb_fvg      else
                              "〰️ proche 🔴"   if nr_fvg      else
                              "❌ Pas de FVG"),
            "HMA Couleur":   hma_color,
            "ATR M15":       atr_val,
            "ADX":           adx_val,
            "+DI / -DI":     f"{pdi_val} / {mdi_val}",
            "Confirmations": confirm_str,
            "Manque":        missing_str,
            "Qualité":       quality,
            "Prix":          round(price, 5),
        }

    except Exception as e:
        st.warning(f"⚠️ Erreur {ticker} : {e}")
        return None


# ----------------------------------------------------------------
#  FETCH OANDA
# ----------------------------------------------------------------
def fetch_oanda_data(client, instrument, granularity, count):
    try:
        r = instruments.InstrumentsCandles(
            instrument=instrument,
            params={"count": count, "granularity": granularity}
        )
        client.request(r)
        rows = [
            {
                "time":  pd.to_datetime(c["time"]),
                "open":  float(c["mid"]["o"]),
                "high":  float(c["mid"]["h"]),
                "low":   float(c["mid"]["l"]),
                "close": float(c["mid"]["c"]),
            }
            for c in r.response.get("candles", []) if c["complete"]
        ]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).set_index("time")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df
    except Exception as e:
        print(f"[OANDA] {instrument} {granularity} : {e}")
        return pd.DataFrame()


# ----------------------------------------------------------------
#  INTERFACE STREAMLIT
# ----------------------------------------------------------------
def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 — ICT Signal Scanner")
    st.caption(
        "Signal = HMA 20 flip couleur (≤ 30 min) → confirmé par Biais Daily + Zone + FVG M15"
    )

    if "OANDA_ACCESS_TOKEN" not in st.secrets:
        st.error("ERREUR : Clé API OANDA manquante dans les secrets Streamlit.")
        st.stop()

    client = oandapyV20.API(
        access_token=st.secrets["OANDA_ACCESS_TOKEN"],
        environment="practice"
    )

    assets = [
        "EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD",
        "AUD_USD", "XAU_USD", "NAS100_USD", "GBP_CHF", "CAD_CHF"
    ]

    with st.expander("📘 Logique du signal"):
        st.markdown("""
        ### 🔑 Le signal vient de la HMA — pas du biais

        | Étape | Rôle | Détail |
        |---|---|---|
        | **1 — TRIGGER** | HMA 20 M15 flip couleur | Rouge → Vert = signal LONG possible / Vert → Rouge = signal SHORT possible |
        | **2 — FRAÎCHEUR** | Signal ≤ 30 min | Flip détecté sur les 2 dernières bougies M15 maximum |
        | **3 — BIAIS** | Confirmation Daily | BULLISH si LONG, BEARISH si SHORT (EMA21 > EMA50) |
        | **4 — ZONE** | Confirmation structurelle | DISCOUNT + proche PDL (LONG) / PREMIUM + proche PDH (SHORT) |
        | **5 — FVG M15** | Confirmation imbalance | Prix dans un FVG vert (LONG) / rouge (SHORT) |
        | **6 — MOMENTUM** | Bonus LONG uniquement | ATR actif + ADX > 20 avec +DI > -DI |

        > ⚡ La colonne **Confirmations** montre ce qui valide le signal.
        > La colonne **Manque** montre ce qui bloque le A+.
        """)

    col1, col2 = st.columns([3, 1])
    with col1:
        run = st.button("🚀 LANCER LE SCANNER", use_container_width=True)
    with col2:
        freshness = st.selectbox("Fraîcheur max", [15, 30, 45, 60], index=1, key="fresh")

    if run:
        # Mise à jour dynamique de la limite de fraîcheur
        fresh_candles = freshness // 15

        results  = []
        progress = st.progress(0)
        status   = st.empty()

        with st.spinner("Analyse en cours..."):
            for i, ticker in enumerate(assets):
                status.caption(f"⏳ Analyse {ticker}...")
                res = analyze_asset(client, ticker)

                # Re-appliquer la limite de fraîcheur choisie par l'utilisateur
                if res:
                    # Recalculer fraîcheur avec seuil choisi
                    min_ago_str = res["Fraîcheur"].replace("⚡ ", "").replace("⏳ ", "").replace(" min", "").strip()
                    try:
                        min_ago = int(min_ago_str)
                        if min_ago <= freshness:
                            res["Fraîcheur"] = f"⚡ {min_ago} min"
                        else:
                            res["Fraîcheur"] = f"⏳ {min_ago} min"
                            # Signal expiré → pas A+
                            if "A+" in res["Qualité"]:
                                res["Qualité"] = "IGNORE"
                    except:
                        pass
                    results.append(res)

                time.sleep(0.2)
                progress.progress((i + 1) / len(assets))

        status.empty()

        if results:
            df = pd.DataFrame(results)

            # A+ en tête, puis signaux frais, puis le reste
            def sort_key(row):
                if "A+" in row["Qualité"]:         return 0
                if "⚡" in str(row["Fraîcheur"]):  return 1
                return 2

            df["_s"] = df.apply(sort_key, axis=1)
            df = df.sort_values("_s").drop(columns=["_s"]).reset_index(drop=True)

            def style_row(row):
                s   = [""] * len(row)
                idx = row.index.tolist()
                def si(col): return idx.index(col)

                # Qualité
                if "A+" in str(row["Qualité"]):
                    s[si("Qualité")] = "background-color:#004d00;color:#00ff88;font-weight:bold"
                else:
                    s[si("Qualité")] = "color:#444"

                # Signal HMA
                sig = str(row["Signal HMA"])
                if "LONG" in sig and "expiré" not in sig:
                    s[si("Signal HMA")] = "color:#00ff88;font-weight:bold"
                elif "SHORT" in sig and "expiré" not in sig:
                    s[si("Signal HMA")] = "color:#ff4b4b;font-weight:bold"
                else:
                    s[si("Signal HMA")] = "color:#555"

                # Fraîcheur
                if "⚡" in str(row["Fraîcheur"]):
                    s[si("Fraîcheur")] = "color:#ffd700;font-weight:bold"
                else:
                    s[si("Fraîcheur")] = "color:#555"

                # Biais Daily
                if "BULLISH" in str(row["Biais Daily"]):
                    s[si("Biais Daily")] = "color:#00ff88"
                elif "BEARISH" in str(row["Biais Daily"]):
                    s[si("Biais Daily")] = "color:#ff4b4b"

                # Zone
                if "DISCOUNT" in str(row["Zone"]):
                    s[si("Zone")] = "color:#00ccff"
                elif "PREMIUM" in str(row["Zone"]):
                    s[si("Zone")] = "color:#ff9900"

                # FVG
                if "Dans FVG" in str(row["FVG M15"]):
                    s[si("FVG M15")] = "color:#00ff88;font-weight:bold"
                elif "proche" in str(row["FVG M15"]):
                    s[si("FVG M15")] = "color:#ffd700"
                else:
                    s[si("FVG M15")] = "color:#555"

                # Confirmations
                s[si("Confirmations")] = "color:#00ff88;font-size:0.85em"
                s[si("Manque")]        = "color:#ff6666;font-size:0.85em"

                # ADX
                try:
                    v = float(row["ADX"])
                    s[si("ADX")] = (
                        "color:#00ff88" if v > 25 else
                        "color:#ffd700" if v > 20 else
                        "color:#ff4b4b"
                    )
                except:
                    pass

                return s

            st.dataframe(df.style.apply(style_row, axis=1), use_container_width=True)

            valid = len(df[df["Qualité"] == "💎 A+ SETUP"])
            fresh = len(df[df["Fraîcheur"].str.contains("⚡", na=False)])

            col_a, col_b = st.columns(2)
            with col_a:
                if valid:
                    st.success(f"🎯 {valid} Setup(s) A+ détecté(s) !")
                else:
                    st.info("Aucun setup A+ pour l'instant.")
            with col_b:
                if fresh:
                    st.warning(f"⚡ {fresh} signal(s) HMA frais (< {freshness} min) — confirmations incomplètes.")

        else:
            st.warning("Aucun résultat — vérifie la connexion OANDA.")


if __name__ == "__main__":
    main()
