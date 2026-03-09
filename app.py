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
#  SIGNAL  = HMA 20 M15 flip de couleur  (≤ 30 min)
#  SCORE   = système de notation pondéré /100
#  GRADE   = A+ / A / B+ / B / C  affiché à côté de l'actif
# ================================================================


# ----------------------------------------------------------------
#  SYSTÈME DE NOTATION  (inspiré du scoring manuel ICT/SMC)
#
#  Les traders manuels pèsent chaque condition selon son importance :
#  - Le trigger (HMA flip frais) est le plus important : sans lui, pas de trade
#  - Le biais Daily et la zone sont les fondations structurelles
#  - Le FVG confirme la zone d'intérêt précise
#  - ADX/ATR confirment le momentum
#
#  Points max = 100
#
#  TRIGGER (30 pts max)
#    HMA flip ≤ 15 min  → 30 pts   (signal ultra-frais)
#    HMA flip ≤ 30 min  → 20 pts   (signal frais)
#    HMA flip ≤ 45 min  → 10 pts   (signal qui vieillit)
#    HMA flip > 45 min  →  0 pts   (expiré)
#
#  BIAIS DAILY concordant  → 20 pts
#
#  ZONE D'INTÉRÊT (20 pts max)
#    Discount + proche PDL (LONG)  → 20 pts
#    Discount seulement            → 10 pts
#    Premium + proche PDH (SHORT)  → 20 pts
#    Premium seulement             → 10 pts
#
#  FVG M15 (15 pts max)
#    Prix dans le FVG              → 15 pts
#    FVG proche (< 1 ATR)          →  7 pts
#
#  ADX + momentum (10 pts max)
#    ADX > 25 + DI concordant      → 10 pts
#    ADX > 20 + DI concordant      →  6 pts
#    ADX > 20 seulement            →  3 pts
#
#  ATR actif (5 pts max)
#    ATR ≥ moyenne                 →  5 pts
#    ATR ≥ 50% moyenne             →  3 pts
#
#  GRADE :
#    A+  : 85-100  (setup parfait, tout aligné)
#    A   : 70-84   (très bon setup, 1 élément mineur manque)
#    B+  : 55-69   (bon setup en formation, surveiller)
#    B   : 40-54   (partiel, attendre confirmation)
#    C   : < 40    (faible, ne pas trader)
# ----------------------------------------------------------------

def compute_score(flip_type, candles_ago, bias, zone_discount, zone_premium,
                  near_pdl, near_pdh, below_mid, above_mid,
                  in_bull_fvg, in_bear_fvg, fvg_near_bull, fvg_near_bear,
                  adx_val, pdi_val, mdi_val, atr_val, atr_mean):

    score        = 0
    score_detail = {}

    # ── TRIGGER ──────────────────────────────────────────────────
    if flip_type is not None and candles_ago is not None:
        mins = candles_ago * 15
        if   mins <= 15: pts = 30
        elif mins <= 30: pts = 20
        elif mins <= 45: pts = 10
        else:            pts = 0
    else:
        pts = 0
    score += pts
    score_detail["Trigger"] = pts

    # Direction du signal HMA
    signal_is_bull = (flip_type == "BULL")
    signal_is_bear = (flip_type == "BEAR")

    # ── BIAIS DAILY ──────────────────────────────────────────────
    bias_ok = (signal_is_bull and bias == "BULLISH") or (signal_is_bear and bias == "BEARISH")
    pts = 20 if bias_ok else 0
    score += pts
    score_detail["Biais"] = pts

    # ── ZONE D'INTÉRÊT ────────────────────────────────────────────
    if signal_is_bull:
        if zone_discount:   pts = 20
        elif below_mid:     pts = 10
        else:               pts = 0
    elif signal_is_bear:
        if zone_premium:    pts = 20
        elif above_mid:     pts = 10
        else:               pts = 0
    else:
        pts = 0
    score += pts
    score_detail["Zone"] = pts

    # ── FVG M15 ───────────────────────────────────────────────────
    if signal_is_bull:
        if in_bull_fvg:     pts = 15
        elif fvg_near_bull: pts = 7
        else:               pts = 0
    elif signal_is_bear:
        if in_bear_fvg:     pts = 15
        elif fvg_near_bear: pts = 7
        else:               pts = 0
    else:
        pts = 0
    score += pts
    score_detail["FVG"] = pts

    # ── ADX / MOMENTUM ────────────────────────────────────────────
    adx_dir_bull = pdi_val > mdi_val
    adx_dir_bear = mdi_val > pdi_val
    adx_dir_ok   = (signal_is_bull and adx_dir_bull) or (signal_is_bear and adx_dir_bear)

    if   adx_val > 25 and adx_dir_ok: pts = 10
    elif adx_val > 20 and adx_dir_ok: pts = 6
    elif adx_val > 20:                pts = 3
    else:                             pts = 0
    score += pts
    score_detail["ADX"] = pts

    # ── ATR ───────────────────────────────────────────────────────
    if   atr_val >= atr_mean:         pts = 5
    elif atr_val >= atr_mean * 0.5:   pts = 3
    else:                             pts = 0
    score += pts
    score_detail["ATR"] = pts

    # ── GRADE ─────────────────────────────────────────────────────
    if   score >= 85: grade = "A+"
    elif score >= 70: grade = "A"
    elif score >= 55: grade = "B+"
    elif score >= 40: grade = "B"
    else:             grade = "C"

    return score, grade, score_detail


def grade_badge(grade, score):
    """Formate le badge affiché dans la colonne Actif."""
    badges = {
        "A+": f"💎 A+  [{score}/100]",
        "A":  f"🥇 A   [{score}/100]",
        "B+": f"🥈 B+  [{score}/100]",
        "B":  f"🔵 B   [{score}/100]",
        "C":  f"⚪ C   [{score}/100]",
    }
    return badges.get(grade, f"— [{score}/100]")


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
#  BIAIS DAILY
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
#  FLIP HMA — dernier changement de couleur
# ----------------------------------------------------------------
def find_last_hma_flip(hma_series, max_lookback=10):
    colors = []
    for i in range(len(hma_series) - 1,
                   max(len(hma_series) - max_lookback - 2, 1), -1):
        v_curr = hma_series.iloc[i]
        v_prev = hma_series.iloc[i - 1]
        if pd.isna(v_curr) or pd.isna(v_prev):
            continue
        colors.append((i, "GREEN" if v_curr > v_prev else "RED"))

    for j in range(len(colors) - 1):
        idx_curr, col_curr = colors[j]
        _,        col_prev = colors[j + 1]
        if col_curr != col_prev:
            candles_ago = (len(hma_series) - 1) - idx_curr
            return ("BULL" if col_curr == "GREEN" else "BEAR"), candles_ago

    return None, None


# ----------------------------------------------------------------
#  FVG M15
# ----------------------------------------------------------------
def detect_fvg(df, price, lookback=80):
    sub = df.iloc[-(lookback + 3):-1]
    bull_fvgs, bear_fvgs = [], []

    for i in range(2, len(sub)):
        lo = sub['high'].iloc[i - 2];  hi = sub['low'].iloc[i]
        if hi > lo: bull_fvgs.append((lo, hi))

        lo2 = sub['high'].iloc[i];  hi2 = sub['low'].iloc[i - 2]
        if hi2 > lo2: bear_fvgs.append((lo2, hi2))

    bull_fvgs.sort(key=lambda x: abs(price - (x[0] + x[1]) / 2))
    bear_fvgs.sort(key=lambda x: abs(price - (x[0] + x[1]) / 2))

    in_bull = any(lo <= price <= hi  for lo, hi in bull_fvgs)
    in_bear = any(lo <= price <= hi  for lo, hi in bear_fvgs)

    # "Proche" = FVG le plus proche dans 1 ATR
    return (in_bull, in_bear,
            bull_fvgs[0] if bull_fvgs else None,
            bear_fvgs[0] if bear_fvgs else None)


# ----------------------------------------------------------------
#  ANALYSE PRINCIPALE
# ----------------------------------------------------------------
def analyze_asset(client, ticker, freshness_limit_min=30):
    try:
        df_d   = fetch_oanda_data(client, ticker, "D",   100)
        df_m15 = fetch_oanda_data(client, ticker, "M15", 400)

        if df_d.empty or df_m15.empty:
            return None

        price = df_m15['close'].iloc[-1]

        # ── BIAIS DAILY ───────────────────────────────────────────
        bias = get_daily_bias(df_d)

        # ── PDH / PDL ─────────────────────────────────────────────
        pdh = df_d['high'].iloc[-2]
        pdl = df_d['low'].iloc[-2]

        # ── MIDNIGHT OPEN NY ──────────────────────────────────────
        ny_tz    = pytz.timezone('America/New_York')
        df_m15.index = df_m15.index.tz_convert(ny_tz)
        today_ny = datetime.now(ny_tz).date()

        mask   = ((df_m15.index.date == today_ny) &
                  (df_m15.index.hour == 0) & (df_m15.index.minute == 0))
        mid_c  = df_m15[mask]
        m_open = mid_c['open'].iloc[0] if not mid_c.empty else df_m15['open'].iloc[0]

        # ── ZONE ──────────────────────────────────────────────────
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
        atr_m15  = QuantEngine.atr(df_m15, 14)
        atr_val  = atr_m15.iloc[-1]
        atr_mean = atr_m15.iloc[-50:].mean()

        in_bull_fvg, in_bear_fvg, nb_fvg, nr_fvg = detect_fvg(df_m15, price, lookback=80)

        # FVG "proche" = gap le plus proche à moins de 1 ATR M15
        fvg_near_bull = nb_fvg is not None and abs(price - (nb_fvg[0] + nb_fvg[1]) / 2) < atr_val
        fvg_near_bear = nr_fvg is not None and abs(price - (nr_fvg[0] + nr_fvg[1]) / 2) < atr_val

        # ── HMA FLIP ──────────────────────────────────────────────
        hma = QuantEngine.hma(df_m15['close'], 20)
        if hma.isna().iloc[-5:].any():
            return None

        flip_type, candles_ago = find_last_hma_flip(hma, max_lookback=10)

        mins_ago      = (candles_ago * 15) if candles_ago is not None else 999
        signal_fresh  = flip_type is not None and mins_ago <= freshness_limit_min
        freshness_str = (f"⚡ {mins_ago} min" if signal_fresh
                         else f"⏳ {mins_ago} min" if flip_type else "—")

        hma_color = "🟢 VERT" if (hma.iloc[-1] > hma.iloc[-2]) else "🔴 ROUGE"

        # ── ADX ───────────────────────────────────────────────────
        adx_s, pdi_s, mdi_s = QuantEngine.adx(df_m15, 14)
        adx_val = round(adx_s.iloc[-1], 1)
        pdi_val = round(pdi_s.iloc[-1], 1)
        mdi_val = round(mdi_s.iloc[-1], 1)

        # ── SCORE & GRADE ─────────────────────────────────────────
        score, grade, score_detail = compute_score(
            flip_type, candles_ago,
            bias,
            zone_discount, zone_premium,
            near_pdl, near_pdh,
            below_mid, above_mid,
            in_bull_fvg, in_bear_fvg,
            fvg_near_bull, fvg_near_bear,
            adx_val, pdi_val, mdi_val,
            atr_val, atr_mean
        )

        badge = grade_badge(grade, score)

        # ── SIGNAL FINAL ──────────────────────────────────────────
        setup_valid = grade in ("A+", "A") and signal_fresh
        quality     = "💎 A+ SETUP" if grade == "A+" and signal_fresh else \
                      "🥇 A SETUP"  if grade == "A"  and signal_fresh else \
                      "👀 SURVEILLER" if grade in ("B+", "B") and signal_fresh else \
                      "IGNORE"

        if signal_fresh:
            sig = "🟢 LONG"  if flip_type == "BULL" else "🔴 SHORT"
        elif flip_type == "BULL":
            sig = "🟢 LONG (expiré)"
        elif flip_type == "BEAR":
            sig = "🔴 SHORT (expiré)"
        else:
            sig = "—"

        # Résumé détail score
        detail_str = (
            f"Trig:{score_detail['Trigger']} "
            f"Biais:{score_detail['Biais']} "
            f"Zone:{score_detail['Zone']} "
            f"FVG:{score_detail['FVG']} "
            f"ADX:{score_detail['ADX']} "
            f"ATR:{score_detail['ATR']}"
        )

        return {
            "Actif + Note":  f"{ticker}  {badge}",
            "Signal":        sig,
            "Fraîcheur":     freshness_str,
            "Score /100":    score,
            "Grade":         grade,
            "Biais Daily":   bias,
            "Zone":          zone_label,
            "FVG M15":       ("✅ Dans FVG 🟢" if in_bull_fvg else
                              "✅ Dans FVG 🔴" if in_bear_fvg else
                              "〰️ proche 🟢"   if fvg_near_bull else
                              "〰️ proche 🔴"   if fvg_near_bear else
                              "❌ Pas de FVG"),
            "HMA":           hma_color,
            "ADX":           adx_val,
            "+DI/-DI":       f"{pdi_val}/{mdi_val}",
            "Détail score":  detail_str,
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
            {"time":  pd.to_datetime(c["time"]),
             "open":  float(c["mid"]["o"]),
             "high":  float(c["mid"]["h"]),
             "low":   float(c["mid"]["l"]),
             "close": float(c["mid"]["c"])}
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
#  INTERFACE
# ----------------------------------------------------------------
def main():
    st.set_page_config(page_title="BLUESTAR SNIPER V10", layout="wide")
    st.title("🎯 BLUESTAR SNIPER V10 — ICT Signal Scanner")
    st.caption("Signal = HMA 20 flip | Score /100 | Grade A+ → C")

    if "OANDA_ACCESS_TOKEN" not in st.secrets:
        st.error("Clé API OANDA manquante.")
        st.stop()

    client = oandapyV20.API(
        access_token=st.secrets["OANDA_ACCESS_TOKEN"],
        environment="practice"
    )

    assets = [
        "EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD",
        "AUD_USD", "XAU_USD", "NAS100_USD", "GBP_CHF", "CAD_CHF"
    ]

    with st.expander("📘 Grille de notation — comment le score est calculé"):
        st.markdown("""
        | Critère | Max | Détail |
        |---|---|---|
        | **Trigger HMA flip** | 30 pts | ≤15 min = 30 · ≤30 min = 20 · ≤45 min = 10 · >45 min = 0 |
        | **Biais Daily** | 20 pts | Concordant avec le flip = 20 · Opposé = 0 |
        | **Zone d'intérêt** | 20 pts | Discount+PDL ou Premium+PDH = 20 · Zone seule = 10 · Hors zone = 0 |
        | **FVG M15** | 15 pts | Dans le FVG = 15 · FVG proche = 7 · Absent = 0 |
        | **ADX momentum** | 10 pts | ADX>25+DI ok = 10 · ADX>20+DI ok = 6 · ADX>20 seul = 3 |
        | **ATR actif** | 5 pts | ≥ moyenne = 5 · ≥ 50% moyenne = 3 · Plat = 0 |

        | Grade | Score | Interprétation |
        |---|---|---|
        | 💎 **A+** | 85-100 | Tout aligné — trader immédiatement |
        | 🥇 **A**  | 70-84  | Très bon — 1 élément mineur manque |
        | 🥈 **B+** | 55-69  | En formation — surveiller, attendre |
        | 🔵 **B**  | 40-54  | Partiel — pas encore tradable |
        | ⚪ **C**  | < 40   | Faible — ignorer |
        """)

    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        run = st.button("🚀 LANCER LE SCANNER", use_container_width=True)
    with col2:
        freshness = st.selectbox("Fraîcheur max", [15, 30, 45, 60], index=1)
    with col3:
        min_grade = st.selectbox("Grade min affiché", ["Tous", "B", "B+", "A", "A+"], index=0)

    if run:
        results  = []
        progress = st.progress(0)
        status   = st.empty()

        with st.spinner("Analyse en cours..."):
            for i, ticker in enumerate(assets):
                status.caption(f"⏳ {ticker}...")
                res = analyze_asset(client, ticker, freshness_limit_min=freshness)
                if res:
                    results.append(res)
                time.sleep(0.2)
                progress.progress((i + 1) / len(assets))

        status.empty()

        if not results:
            st.warning("Aucun résultat — vérifie la connexion OANDA.")
            return

        df = pd.DataFrame(results)

        # ── FILTRE PAR GRADE MIN ──────────────────────────────────
        grade_order = {"A+": 5, "A": 4, "B+": 3, "B": 2, "C": 1}
        min_map     = {"Tous": 0, "B": 2, "B+": 3, "A": 4, "A+": 5}
        min_val     = min_map[min_grade]
        df = df[df["Grade"].map(grade_order) >= min_val]

        # ── TRI : A+ frais → A frais → B+ frais → ... → score décroissant
        def sort_key(row):
            fresh = 1 if "⚡" in str(row["Fraîcheur"]) else 0
            g     = grade_order.get(row["Grade"], 0)
            s     = row["Score /100"]
            return (-fresh, -g, -s)

        df["_sk"] = df.apply(sort_key, axis=1)
        df = df.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)

        # ── STYLE ─────────────────────────────────────────────────
        def style_row(row):
            s   = [""] * len(row)
            idx = row.index.tolist()
            def si(col):
                return idx.index(col) if col in idx else -1

            grade = row["Grade"]
            fresh = "⚡" in str(row["Fraîcheur"])

            # Actif + Note
            i = si("Actif + Note")
            if   grade == "A+" and fresh: s[i] = "background-color:#004d00;color:#00ff88;font-weight:bold;font-size:1.05em"
            elif grade == "A"  and fresh: s[i] = "background-color:#003322;color:#66ffaa;font-weight:bold"
            elif grade in ("B+","B") and fresh: s[i] = "background-color:#1a1a2e;color:#aaccff"
            else:                          s[i] = "color:#555"

            # Score /100
            i = si("Score /100")
            sc = row["Score /100"]
            if   sc >= 85: s[i] = "color:#00ff88;font-weight:bold"
            elif sc >= 70: s[i] = "color:#66ffaa"
            elif sc >= 55: s[i] = "color:#ffd700"
            elif sc >= 40: s[i] = "color:#ff9944"
            else:          s[i] = "color:#555"

            # Signal
            i = si("Signal")
            sig = str(row["Signal"])
            if "LONG" in sig and "expiré" not in sig:   s[i] = "color:#00ff88;font-weight:bold"
            elif "SHORT" in sig and "expiré" not in sig: s[i] = "color:#ff4b4b;font-weight:bold"
            else:                                         s[i] = "color:#555"

            # Fraîcheur
            i = si("Fraîcheur")
            s[i] = "color:#ffd700;font-weight:bold" if "⚡" in str(row["Fraîcheur"]) else "color:#555"

            # Biais
            i = si("Biais Daily")
            if "BULLISH" in str(row["Biais Daily"]):  s[i] = "color:#00ff88"
            elif "BEARISH" in str(row["Biais Daily"]): s[i] = "color:#ff4b4b"

            # Zone
            i = si("Zone")
            if "DISCOUNT" in str(row["Zone"]):  s[i] = "color:#00ccff"
            elif "PREMIUM" in str(row["Zone"]): s[i] = "color:#ff9900"

            # FVG
            i = si("FVG M15")
            if "Dans FVG" in str(row["FVG M15"]):  s[i] = "color:#00ff88;font-weight:bold"
            elif "proche"  in str(row["FVG M15"]):  s[i] = "color:#ffd700"
            else:                                    s[i] = "color:#555"

            # ADX
            i = si("ADX")
            try:
                v = float(row["ADX"])
                s[i] = ("color:#00ff88" if v > 25 else
                         "color:#ffd700" if v > 20 else
                         "color:#ff4b4b")
            except: pass

            # Qualité
            i = si("Qualité")
            if "A+ SETUP"    in str(row["Qualité"]): s[i] = "background-color:#004d00;color:#00ff88;font-weight:bold"
            elif "A SETUP"   in str(row["Qualité"]): s[i] = "color:#66ffaa;font-weight:bold"
            elif "SURVEILLER" in str(row["Qualité"]): s[i] = "color:#ffd700"
            else:                                      s[i] = "color:#444"

            # Détail score (petit, discret)
            i = si("Détail score")
            s[i] = "color:#444;font-size:0.78em"

            return s

        st.dataframe(df.style.apply(style_row, axis=1), use_container_width=True)

        # ── RÉSUMÉ ────────────────────────────────────────────────
        a_plus  = len(df[(df["Grade"] == "A+") & df["Fraîcheur"].str.contains("⚡", na=False)])
        a_grade = len(df[(df["Grade"] == "A")  & df["Fraîcheur"].str.contains("⚡", na=False)])
        watch   = len(df[df["Grade"].isin(["B+", "B"]) & df["Fraîcheur"].str.contains("⚡", na=False)])

        cols = st.columns(3)
        with cols[0]:
            if a_plus:  st.success(f"💎 {a_plus} setup(s) A+ actif(s)")
            else:       st.info("Aucun A+ pour l'instant")
        with cols[1]:
            if a_grade: st.success(f"🥇 {a_grade} setup(s) A actif(s)")
        with cols[2]:
            if watch:   st.warning(f"👀 {watch} setup(s) B/B+ à surveiller")


if __name__ == "__main__":
    main()
