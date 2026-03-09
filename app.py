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
#  Signal LONG  → Biais Daily BULLISH
#                + Zone DISCOUNT (prix < Midnight Open ET proche PDL)
#                + Prix dans un FVG Bullish M15
#                + HMA 20 M15 flip au VERT  ← TRIGGER
#                + ATR actif + ADX haussier
#
#  Signal SHORT → Biais Daily BEARISH
#                + Zone PREMIUM (prix > Midnight Open ET proche PDH)
#                + Prix dans un FVG Bearish M15
#                + HMA 20 M15 flip au ROUGE ← TRIGGER
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
        c = df['close'].shift(1)
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
#  DETECTION FVG M15
#
#  FVG Bullish : high[i-2] < low[i]   → gap vert entre bougie i-2 et i
#  FVG Bearish : low[i-2]  > high[i]  → gap rouge entre bougie i-2 et i
#
#  On scanne les 80 dernières bougies.
#  On retourne si le prix actuel est à l'intérieur d'un FVG.
# ----------------------------------------------------------------
def detect_fvg(df, price, lookback=80):
    sub = df.iloc[-(lookback + 3):-1]   # on exclut la bougie en cours

    bull_fvgs = []
    bear_fvgs = []

    for i in range(2, len(sub)):
        # Bullish FVG : low[i] > high[i-2]
        lo = sub['high'].iloc[i - 2]
        hi = sub['low'].iloc[i]
        if hi > lo:
            bull_fvgs.append((lo, hi))

        # Bearish FVG : high[i] < low[i-2]
        lo2 = sub['high'].iloc[i]
        hi2 = sub['low'].iloc[i - 2]
        if hi2 > lo2:
            bear_fvgs.append((lo2, hi2))

    # Tri par proximité du prix
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
        # Données
        df_d   = fetch_oanda_data(client, ticker, "D",   100)   # biais daily
        df_m15 = fetch_oanda_data(client, ticker, "M15", 400)   # signal M15

        if df_d.empty or df_m15.empty:
            return None

        price = df_m15['close'].iloc[-1]

        # 1. BIAIS DAILY ──────────────────────────────────────────
        bias = get_daily_bias(df_d)

        # 2. PDH / PDL (jour précédent) ───────────────────────────
        pdh = df_d['high'].iloc[-2]
        pdl = df_d['low'].iloc[-2]

        # 3. MIDNIGHT OPEN (00:00 NY) ─────────────────────────────
        ny_tz = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        today_ny = datetime.now(ny_tz).date()

        mask = (
            (df_m15.index.date   == today_ny) &
            (df_m15.index.hour   == 0) &
            (df_m15.index.minute == 0)
        )
        mid_c  = df_m15[mask]
        m_open = mid_c['open'].iloc[0] if not mid_c.empty else df_m15['open'].iloc[0]

        below_mid = price < m_open
        above_mid = price > m_open

        # 4. ZONE D'INTÉRÊT ───────────────────────────────────────
        #
        #  DISCOUNT (LONG) :
        #    → prix sous le Midnight Open  (moitié basse de la journée)
        #    → prix dans 1 ATR Daily au-dessus du PDL  (zone de liquidité)
        #
        #  PREMIUM (SHORT) :
        #    → prix au-dessus du Midnight Open
        #    → prix dans 1 ATR Daily en-dessous du PDH
        #
        atr_d = QuantEngine.atr(df_d, 14).iloc[-1]

        near_pdl      = price <= (pdl + atr_d)
        near_pdh      = price >= (pdh - atr_d)
        zone_discount = below_mid and near_pdl
        zone_premium  = above_mid and near_pdh

        zone_label = (
            "📉 DISCOUNT" if zone_discount else
            "📈 PREMIUM"  if zone_premium  else
            "〰️ NEUTRE"
        )

        # 5. FVG M15 ──────────────────────────────────────────────
        in_bull_fvg, in_bear_fvg, nb_fvg, nr_fvg = detect_fvg(df_m15, price, lookback=80)

        fvg_label = (
            "✅ Dans FVG 🟢" if in_bull_fvg else
            "✅ Dans FVG 🔴" if in_bear_fvg else
            "〰️ FVG 🟢 proche" if nb_fvg else
            "〰️ FVG 🔴 proche" if nr_fvg else
            "❌ Pas de FVG"
        )

        # 6. HMA 20 M15 — FLIP DE COULEUR ─────────────────────────
        hma = QuantEngine.hma(df_m15['close'], 20)
        if hma.isna().iloc[-3:].any():
            return None

        h1, h2, h3 = hma.iloc[-1], hma.iloc[-2], hma.iloc[-3]
        color_now  = "GREEN" if h1 > h2 else "RED"
        color_prev = "GREEN" if h2 > h3 else "RED"

        flip_bull = (color_now == "GREEN") and (color_prev == "RED")
        flip_bear = (color_now == "RED")   and (color_prev == "GREEN")

        # 7. ATR + ADX M15 ────────────────────────────────────────
        atr_m15    = QuantEngine.atr(df_m15, 14)
        atr_val    = round(atr_m15.iloc[-1], 5)
        atr_active = atr_val >= atr_m15.iloc[-50:].mean() * 0.5

        adx_s, pdi_s, mdi_s = QuantEngine.adx(df_m15, 14)
        adx_val = round(adx_s.iloc[-1], 1)
        pdi_val = round(pdi_s.iloc[-1], 1)
        mdi_val = round(mdi_s.iloc[-1], 1)
        adx_bull = adx_val > 20 and pdi_val > mdi_val
        adx_bear = adx_val > 20 and mdi_val > pdi_val

        # 8. VALIDATION A+ SETUP ──────────────────────────────────
        setup_valid = False
        signal_type = bias
        trigger_ok  = False
        fvg_ok      = False

        # LONG : biais Daily BULLISH + DISCOUNT + dans FVG vert + HMA flip vert + momentum
        if (bias == "BULLISH"
                and zone_discount
                and in_bull_fvg
                and flip_bull
                and atr_active
                and adx_bull):
            setup_valid = True
            signal_type = "BULLISH"
            trigger_ok  = True
            fvg_ok      = True

        # SHORT : biais Daily BEARISH + PREMIUM + dans FVG rouge + HMA flip rouge
        elif (bias == "BEARISH"
                and zone_premium
                and in_bear_fvg
                and flip_bear):
            setup_valid = True
            signal_type = "BEARISH"
            trigger_ok  = True
            fvg_ok      = True

        quality = "💎 A+ SETUP" if setup_valid else "IGNORE"

        return {
            "Actif":        ticker,
            "Biais Daily":  bias,
            "Zone":         zone_label,
            "Midnight":     round(m_open, 5),
            "PDL / PDH":    f"{round(pdl,5)} / {round(pdh,5)}",
            "FVG M15":      fvg_label,
            "HMA Couleur":  "🟢 VERT" if color_now == "GREEN" else "🔴 ROUGE",
            "HMA Flip":     "✅ FLIP !" if trigger_ok else f"〰️ {color_now}",
            "ATR M15":      atr_val,
            "ADX":          adx_val,
            "+DI / -DI":    f"{pdi_val} / {mdi_val}",
            "Qualité":      quality,
            "Prix":         round(price, 5),
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
        "Biais Daily → Zone d'intérêt (Discount/PDL · Premium/PDH) "
        "→ FVG M15 → HMA 20 M15 flip couleur"
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

    with st.expander("📘 Logique du signal — comment ça marche"):
        st.markdown("""
        ### 🟢 Signal LONG — 6 conditions requises
        | # | Condition | Détail |
        |---|---|---|
        | 1 | **Biais Daily BULLISH** | Prix > EMA21 > EMA50 sur Daily |
        | 2 | **Zone DISCOUNT** | Prix **sous** Midnight Open **ET** à moins de 1 ATR Daily au-dessus du PDL |
        | 3 | **FVG Bullish M15** | Prix actuellement **dans** une imbalance verte M15 |
        | 4 | **HMA 20 flip VERT** ← TRIGGER | HMA vient de changer : rouge → vert sur M15 |
        | 5 | **ATR actif** | ATR M15 ≥ 50% de sa moyenne (marché en mouvement) |
        | 6 | **ADX > 20 + +DI > -DI** | Tendance haussière confirmée sur M15 |

        ### 🔴 Signal SHORT — 4 conditions requises
        | # | Condition | Détail |
        |---|---|---|
        | 1 | **Biais Daily BEARISH** | Prix < EMA21 < EMA50 sur Daily |
        | 2 | **Zone PREMIUM** | Prix **au-dessus** Midnight Open **ET** à moins de 1 ATR Daily en-dessous du PDH |
        | 3 | **FVG Bearish M15** | Prix actuellement **dans** une imbalance rouge M15 |
        | 4 | **HMA 20 flip ROUGE** ← TRIGGER | HMA vient de changer : vert → rouge sur M15 |
        """)

    if st.button("🚀 LANCER LE SCANNER", use_container_width=True):
        results  = []
        progress = st.progress(0)
        status   = st.empty()

        with st.spinner("Analyse en cours..."):
            for i, ticker in enumerate(assets):
                status.caption(f"⏳ Analyse {ticker}...")
                res = analyze_asset(client, ticker)
                if res:
                    results.append(res)
                time.sleep(0.2)
                progress.progress((i + 1) / len(assets))

        status.empty()

        if results:
            df = pd.DataFrame(results)

            df["_s"] = df["Qualité"].apply(lambda x: 0 if "A+" in x else 1)
            df = df.sort_values("_s").drop(columns=["_s"]).reset_index(drop=True)

            def style_row(row):
                s   = [""] * len(row)
                idx = row.index.tolist()
                def si(col): return idx.index(col)

                if "A+" in str(row["Qualité"]):
                    s[si("Qualité")] = "background-color:#004d00;color:#00ff88;font-weight:bold"
                else:
                    s[si("Qualité")] = "color:#444"

                if "BULLISH" in str(row["Biais Daily"]):
                    s[si("Biais Daily")] = "color:#00ff88;font-weight:bold"
                elif "BEARISH" in str(row["Biais Daily"]):
                    s[si("Biais Daily")] = "color:#ff4b4b;font-weight:bold"

                if "DISCOUNT" in str(row["Zone"]):
                    s[si("Zone")] = "color:#00ccff"
                elif "PREMIUM" in str(row["Zone"]):
                    s[si("Zone")] = "color:#ff9900"

                if "Dans FVG" in str(row["FVG M15"]):
                    s[si("FVG M15")] = "color:#00ff88;font-weight:bold"
                elif "proche" in str(row["FVG M15"]):
                    s[si("FVG M15")] = "color:#ffd700"
                else:
                    s[si("FVG M15")] = "color:#555"

                if "✅" in str(row["HMA Flip"]):
                    s[si("HMA Flip")] = "color:#00ff88;font-weight:bold"
                else:
                    s[si("HMA Flip")] = "color:#666"

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
            if valid:
                st.success(f"🎯 {valid} Setup(s) A+ détecté(s) !")
            else:
                st.info(
                    "Aucun setup A+ pour l'instant — "
                    "attendre que le prix soit en zone d'intérêt (FVG + PDL/PDH) "
                    "avec flip HMA dans le bon biais."
                )
        else:
            st.warning("Aucun résultat — vérifie la connexion OANDA.")


if __name__ == "__main__":
    main()
