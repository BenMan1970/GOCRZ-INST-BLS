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
    bias_ok = (signal_is_bull and bias in ("BULLISH","STRONG BULLISH")) or (signal_is_bear and bias in ("BEARISH","STRONG BEARISH"))
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


# ================================================================
#  BIAIS DAILY  —  MÉTHODE INSTITUTIONNELLE MULTI-FACTEURS
#
#  4 facteurs indépendants votent BULLISH / BEARISH / NEUTRAL.
#  Règle stricte : 3 votes minimum dans le même sens pour un biais
#  valide. En dessous → NEUTRAL (marché sans conviction = pas de trade).
#
#  FACTEUR 1 — MARKET STRUCTURE (poids : 2 votes)
#  ─────────────────────────────────────────────────────────────
#  Méthode utilisée par les traders ICT / Smart Money :
#  on identifie les derniers swing highs et swing lows sur le Daily
#  (pivot détecté si la bougie est entourée de 2 bougies plus basses/hautes).
#
#  HH + HL (Higher High + Higher Low) → structure haussière  → BULLISH
#  LH + LL (Lower High  + Lower Low)  → structure baissière  → BEARISH
#  Mélange                            → structure cassée      → NEUTRAL
#
#  C'est le facteur le plus important (2 votes au lieu d'1).
#  Un institutionnel ne trade JAMAIS contre la structure.
#
#  FACTEUR 2 — EMA STACK 21 / 50 DAILY (poids : 1 vote)
#  ─────────────────────────────────────────────────────────────
#  Filtre de tendance classique adopté par les desks macro et les
#  fonds systématiques (CTA, trend-following).
#  Prix > EMA21 > EMA50 → BULLISH
#  Prix < EMA21 < EMA50 → BEARISH
#
#  FACTEUR 3 — WEEKLY OPEN (poids : 1 vote)
#  ─────────────────────────────────────────────────────────────
#  Le niveau d'ouverture de la semaine est la référence numéro 1
#  des market makers et des banques centrales (ICT "Weekly Open").
#  Prix au-dessus du Weekly Open → biais acheteur cette semaine
#  Prix en dessous               → biais vendeur
#
#  FACTEUR 4 — CLOSE DU JOUR PRÉCÉDENT (poids : 1 vote)
#  ─────────────────────────────────────────────────────────────
#  Les institutionnels regardent où le marché a fermé par rapport
#  au milieu de range de la veille (50% du range daily J-1).
#  Close > 50% du range J-1 → journée fermée en force haussière
#  Close < 50% du range J-1 → journée fermée en force baissière
#
#  RÉSULTAT :
#  Total max = 5 votes (2 structure + 1 + 1 + 1)
#  ≥ 4 votes concordants → STRONG BULLISH / STRONG BEARISH
#  = 3 votes concordants → BULLISH / BEARISH
#  ≤ 2 votes             → NEUTRAL  (condition non remplie, pas de trade)
# ================================================================

def _find_swing_points(series, wing=2):
    """
    Retourne les indices des swing highs et swing lows.
    Un swing high[i] = series[i] > tous les wing voisins de chaque côté.
    """
    highs, lows = [], []
    for i in range(wing, len(series) - wing):
        window = series.iloc[i - wing: i + wing + 1]
        if series.iloc[i] == window.max():
            highs.append(i)
        if series.iloc[i] == window.min():
            lows.append(i)
    return highs, lows


def get_daily_bias(df_d):
    """
    Retourne (bias_str, detail_dict).
    bias_str  : "STRONG BULLISH" | "BULLISH" | "NEUTRAL" | "BEARISH" | "STRONG BEARISH"
    detail    : dict avec le vote de chaque facteur pour affichage
    """
    if len(df_d) < 60:
        return "NEUTRAL", {}

    close = df_d['close']
    high  = df_d['high']
    low   = df_d['low']

    votes_bull = 0
    votes_bear = 0
    detail     = {}

    # ── FACTEUR 1 : MARKET STRUCTURE (2 votes) ───────────────────
    sh_idx, sl_idx = _find_swing_points(high, wing=3)
    _,      sl_idx_l = _find_swing_points(low,  wing=3)

    struct_vote = "NEUTRAL"
    if len(sh_idx) >= 2 and len(sl_idx_l) >= 2:
        last_sh  = high.iloc[sh_idx[-1]]
        prev_sh  = high.iloc[sh_idx[-2]]
        last_sl  = low.iloc[sl_idx_l[-1]]
        prev_sl  = low.iloc[sl_idx_l[-2]]

        hh = last_sh > prev_sh   # Higher High
        hl = last_sl > prev_sl   # Higher Low
        lh = last_sh < prev_sh   # Lower High
        ll = last_sl < prev_sl   # Lower Low

        if hh and hl:
            struct_vote = "BULLISH"
            votes_bull += 2
        elif lh and ll:
            struct_vote = "BEARISH"
            votes_bear += 2

    detail["Structure"] = struct_vote

    # ── FACTEUR 2 : EMA STACK 21/50 (1 vote) ────────────────────
    ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    cur   = close.iloc[-1]

    if cur > ema21 > ema50:
        ema_vote = "BULLISH";  votes_bull += 1
    elif cur < ema21 < ema50:
        ema_vote = "BEARISH";  votes_bear += 1
    else:
        ema_vote = "NEUTRAL"

    detail["EMA 21/50"] = ema_vote

    # ── FACTEUR 3 : WEEKLY OPEN (1 vote) ─────────────────────────
    # Cherche la première bougie du lundi de la semaine en cours
    df_d_copy = df_d.copy()
    if df_d_copy.index.tz is not None:
        df_d_copy.index = df_d_copy.index.tz_convert('UTC')

    # Lundi = dayofweek 0
    weekly_open_rows = df_d_copy[df_d_copy.index.dayofweek == 0]
    if not weekly_open_rows.empty:
        weekly_open = weekly_open_rows['open'].iloc[-1]
        if cur > weekly_open:
            wo_vote = "BULLISH";  votes_bull += 1
        else:
            wo_vote = "BEARISH";  votes_bear += 1
    else:
        wo_vote = "NEUTRAL"

    detail["Weekly Open"] = wo_vote

    # ── FACTEUR 4 : CLOSE J-1 vs RANGE J-1 (1 vote) ─────────────
    # Milieu de range = (High J-1 + Low J-1) / 2
    if len(df_d) >= 2:
        prev_high  = high.iloc[-2]
        prev_low   = low.iloc[-2]
        prev_close = close.iloc[-2]
        midpoint   = (prev_high + prev_low) / 2

        if prev_close > midpoint:
            pc_vote = "BULLISH";  votes_bull += 1
        else:
            pc_vote = "BEARISH";  votes_bear += 1
    else:
        pc_vote = "NEUTRAL"

    detail["Close J-1"] = pc_vote

    # ── CONSENSUS ─────────────────────────────────────────────────
    detail["Votes"] = f"{votes_bull}B / {votes_bear}S"

    if   votes_bull >= 4: bias = "STRONG BULLISH"
    elif votes_bull == 3: bias = "BULLISH"
    elif votes_bear >= 4: bias = "STRONG BEARISH"
    elif votes_bear == 3: bias = "BEARISH"
    else:                 bias = "NEUTRAL"

    return bias, detail


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
        df_h1  = fetch_oanda_data(client, ticker, "H1",  100)

        if df_d.empty or df_m15.empty:
            return None

        price = df_m15['close'].iloc[-1]

        # ── BIAIS DAILY (multi-facteurs institutionnel) ─────────
        bias, bias_detail = get_daily_bias(df_d)

        # Règle stricte : HMA flip DOIT être dans le sens du biais daily
        # NEUTRAL = biais non tranché = pas de trade autorisé
        bias_bull  = bias in ("BULLISH", "STRONG BULLISH")
        bias_bear  = bias in ("BEARISH", "STRONG BEARISH")
        bias_valid = bias_bull or bias_bear

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

        # ── ADX H1 (valeur seule ≥ 20) ───────────────────────────
        adx_s, pdi_s, mdi_s = QuantEngine.adx(df_h1, 14)
        adx_val = round(adx_s.iloc[-1], 1)
        pdi_val = round(pdi_s.iloc[-1], 1)   # gardé pour compute_score interne
        mdi_val = round(mdi_s.iloc[-1], 1)   # gardé pour compute_score interne

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

        # ── RÈGLE STRICTE : HMA flip DOIT concorder avec le biais ──
        # C'est la condition bloquante principale.
        # Un flip BULL avec biais BEARISH ou NEUTRAL → signal rejeté.
        # Un flip BEAR avec biais BULLISH ou NEUTRAL → signal rejeté.
        hma_matches_bias = (
            (flip_type == "BULL" and bias_bull) or
            (flip_type == "BEAR" and bias_bear)
        )

        # ── CONDITIONS ÉLIMINATOIRES (toutes strictes) ────────────
        #
        # 1. Biais daily concordant avec le flip HMA
        if not bias_valid or not hma_matches_bias:
            return None

        # 2. Prix du bon côté de la HMA
        #    LONG  → prix AU-DESSUS de la HMA (tendance confirmée)
        #    SHORT → prix EN-DESSOUS de la HMA (tendance confirmée)
        hma_current = hma.iloc[-1]
        price_above_hma = price > hma_current
        price_below_hma = price < hma_current

        if flip_type == "BULL" and not price_above_hma:
            return None
        if flip_type == "BEAR" and not price_below_hma:
            return None

        # 3. FVG dans le bon sens obligatoire
        #    (dans le FVG ou proche — sinon pas de zone d'intérêt valide)
        fvg_long_ok  = in_bull_fvg or fvg_near_bull
        fvg_short_ok = in_bear_fvg or fvg_near_bear

        if flip_type == "BULL" and not fvg_long_ok:
            return None
        if flip_type == "BEAR" and not fvg_short_ok:
            return None

        # Signal frais ET dans le sens du biais
        signal_active = signal_fresh

        # ── SIGNAL FINAL ──────────────────────────────────────────
        if signal_fresh:
            sig = "▲ LONG"  if flip_type == "BULL" else "▼ SHORT"
        elif flip_type == "BULL":
            sig = "LONG (expiré)"
        elif flip_type == "BEAR":
            sig = "SHORT (expiré)"
        else:
            sig = "—"

        quality = (
            "A+ SETUP"   if grade == "A+"  and signal_active else
            "A SETUP"    if grade == "A"   and signal_active else
            "SURVEILLER" if grade in ("B+","B") and signal_active else
            "IGNORE"
        )


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
        # ── 28 paires Forex ──────────────────────────────────────
        # Majeurs (7)
        "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
        "AUD_USD", "USD_CAD", "NZD_USD",
        # Croisés EUR (6)
        "EUR_GBP", "EUR_JPY", "EUR_CHF",
        "EUR_AUD", "EUR_CAD", "EUR_NZD",
        # Croisés GBP (5)
        "GBP_JPY", "GBP_CHF", "GBP_AUD",
        "GBP_CAD", "GBP_NZD",
        # Croisés JPY (4)
        "AUD_JPY", "CAD_JPY", "CHF_JPY", "NZD_JPY",
        # Croisés mineurs (6)
        "AUD_CAD", "AUD_CHF", "AUD_NZD",
        "CAD_CHF", "NZD_CAD", "NZD_CHF",
        # ── Métaux (2) ───────────────────────────────────────────
        "XAU_USD",   # Gold
        "XAG_USD",   # Silver
        # ── Indices (3) ──────────────────────────────────────────
        "US30_USD",  # Dow Jones
        "NAS100_USD",# Nasdaq 100
        "DE30_EUR",  # DAX
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

        # ── RENAME ADX column ─────────────────────────────────────
        if "ADX" in df.columns:
            df = df.rename(columns={"ADX": "ADX H1"})

        # ── TABLE HTML ────────────────────────────────────────────
        # Palette sobre, professionnel — pas de couleurs neon
        # Vert sobre  : #3dba7e   Rouge sobre : #c0392b
        # Jaune sobre : #c8960c   Gris texte  : #8a8a9a
        # Fond ligne A+/A actif   : légère teinte sans agressivité

        GRADE_STYLE = {
            "A+": {"color": "#3dba7e", "bg": "rgba(61,186,126,0.07)", "label": "A+"},
            "A":  {"color": "#5aab8a", "bg": "rgba(90,171,138,0.05)", "label": "A"},
            "B+": {"color": "#a07c30", "bg": "transparent",           "label": "B+"},
            "B":  {"color": "#6a7a9a", "bg": "transparent",           "label": "B"},
            "C":  {"color": "#444455", "bg": "transparent",           "label": "C"},
        }

        def sig_style(s):
            if "LONG"  in s and "expiré" not in s and "🚫" not in s:
                return "color:#3dba7e;font-weight:700;font-size:15px;letter-spacing:.03em"
            if "SHORT" in s and "expiré" not in s and "🚫" not in s:
                return "color:#c0392b;font-weight:700;font-size:15px;letter-spacing:.03em"
            if "🚫" in s or "⚠️" in s:
                return "color:#a05010;font-weight:600"
            return "color:#55556a;font-size:13px"

        def adx_style(v):
            try:
                f = float(v)
                if f >= 25: return "color:#3dba7e;font-weight:700"
                if f >= 20: return "color:#c8960c;font-weight:700"
                return "color:#555566"
            except: return "color:#444"

        def bias_style(b):
            if "BULLISH" in b: return "color:#3dba7e;font-weight:600"
            if "BEARISH" in b: return "color:#c0392b;font-weight:600"
            return "color:#555566"

        def zone_style(z):
            if "DISCOUNT" in z: return "color:#4a8fc0;font-weight:600"
            if "PREMIUM"  in z: return "color:#b07030;font-weight:600"
            return "color:#60607a"

        def fvg_style(f):
            if "Dans FVG" in f: return "color:#3dba7e;font-weight:600"
            if "proche"   in f: return "color:#c8960c"
            return "color:#55556a"

        def fresh_style(f):
            return "color:#c8960c;font-weight:700" if "⚡" in f else "color:#3a3a4a"

        html = """
<style>
.sc-wrap{overflow-x:auto;margin-top:8px}
.sc-tbl{width:100%;border-collapse:collapse;font-family:'IBM Plex Mono','Courier New',monospace;font-size:13px}
.sc-tbl thead tr{border-bottom:1px solid #2a2a3e}
.sc-tbl th{
  padding:10px 16px;text-align:left;color:#7a7aaa;
  font-size:11px;text-transform:uppercase;letter-spacing:.1em;font-weight:600;white-space:nowrap
}
.sc-tbl td{padding:12px 16px;border-bottom:1px solid #161622;vertical-align:middle}
.sc-tbl tr:hover td{background:#0e0e1a}
.ticker{font-size:22px;font-weight:800;color:#e8e8f0;letter-spacing:.04em;white-space:nowrap}
.bias-tag{font-size:12px;font-weight:700;letter-spacing:.06em;vertical-align:middle;margin-left:6px;text-transform:uppercase}
.grade-pill{
  display:inline-block;padding:3px 9px;border-radius:3px;
  font-size:12px;font-weight:700;letter-spacing:.06em;
  border:1px solid;margin-left:10px;vertical-align:middle
}
.score-val{font-size:12px;font-weight:600;margin-left:5px;opacity:.8}
.hma-g{color:#3dba7e;font-weight:600}
.hma-r{color:#c0392b;font-weight:600}
</style>
<div class="sc-wrap"><table class="sc-tbl">
<thead><tr>
  <th>⚡</th>
  <th>Actif</th>
  <th>Signal</th>
  <th>Zone</th>
  <th>ADX H1</th>
</tr></thead><tbody>
"""
        for _, row in df.iterrows():
            grade     = str(row.get("Grade","C"))
            fresh     = "⚡" in str(row.get("Fraîcheur",""))
            gs        = GRADE_STYLE.get(grade, GRADE_STYLE["C"])
            score     = int(row.get("Score /100", 0))
            ticker    = str(row.get("Actif + Note","")).split("  ")[0].strip()
            sig       = str(row.get("Signal","—"))
            adx_v     = str(row.get("ADX H1","—"))
            bias      = str(row.get("Biais Daily","—"))
            zone      = str(row.get("Zone","—"))
            fvg       = str(row.get("FVG M15","—"))
            hma       = str(row.get("HMA","—"))
            fresh_str = str(row.get("Fraîcheur","—"))

            hma_cls = "hma-g" if "VERT" in hma else "hma-r"
            row_bg  = gs["bg"] if fresh and grade in ("A+","A") else "transparent"

            html += f"""<tr style="background:{row_bg}">
  <td style="{fresh_style(fresh_str)};font-size:15px;text-align:center">{fresh_str}</td>
  <td style="white-space:nowrap">
    <span class="ticker">{ticker}</span>
    <span class="bias-tag" style="color:{'#3dba7e' if 'BULLISH' in bias else '#c0392b'}">&nbsp;{'▲' if 'BULLISH' in bias else '▼'} {bias}</span>
    <span class="grade-pill" style="color:{gs['color']};border-color:{gs['color']}">{gs['label']}</span>
    <span class="score-val" style="color:{gs['color']}">{score}</span>
  </td>
  <td style="{sig_style(sig)}">{sig}</td>
  <td style="{zone_style(zone)}">{zone}</td>
  <td style="{adx_style(adx_v)}">{adx_v}</td>
</tr>"""

        html += "</tbody></table></div>"
        st.markdown(html, unsafe_allow_html=True)
        st.markdown("---")

        # ── RÉSUMÉ ────────────────────────────────────────────────
        a_plus  = len(df[(df["Grade"] == "A+") & df["Fraîcheur"].str.contains("⚡", na=False)])
        a_grade = len(df[(df["Grade"] == "A")  & df["Fraîcheur"].str.contains("⚡", na=False)])
        watch   = len(df[df["Grade"].isin(["B+", "B"]) & df["Fraîcheur"].str.contains("⚡", na=False)])

        cols = st.columns(3)
        with cols[0]:
            if a_plus:  st.success(f"💎 {a_plus} setup(s) A+ actif(s)")
            else:       st.info("Aucun signal A+ actif")
        with cols[1]:
            if a_grade: st.success(f"🥇 {a_grade} setup(s) A actif(s)")
        with cols[2]:
            if watch:   st.warning(f"👀 {watch} setup(s) B/B+ à surveiller")



if __name__ == "__main__":
    main()
