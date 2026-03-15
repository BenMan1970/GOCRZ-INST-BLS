import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.accounts as accounts
from datetime import datetime
import pytz
import time
from collections import Counter

# ================================================================
#  BLUESTAR SNIPER V11  —  ICT SIGNAL ENGINE
#  V11 vs V10 :
#  ✅  MTF Institutional Trend (W/D/4H/1H/15m) — score pondéré
#  ✅  Killzone detector (London / NY AM) UTC+1
#  ✅  Daily Bias amélioré (3 facteurs orthogonaux Pine Script)
#  ✅  Score MTF remplace Biais simple (25 pts vs 20 pts)
#  ✅  Colonne MTF % dans le tableau
#  ✅  Badge Killzone sur chaque signal
# ================================================================


# ----------------------------------------------------------------
#  KILLZONE DETECTOR  (UTC+1 — Paris / Tunis)
# ----------------------------------------------------------------
KILLZONES_UTC1 = {
    "London":    (( 8,  0), ( 9, 30)),
    "NY AM":     ((13,  0), (14, 30)),
    "Asia":      (( 2,  0), ( 4,  0)),
}

def get_current_killzone() -> str:
    """Retourne le nom de la killzone active (UTC+1) ou ''."""
    tz_utc1 = pytz.timezone("Europe/Paris")
    now = datetime.now(tz_utc1)
    h, m = now.hour, now.minute
    t = h * 60 + m
    for name, (start, end) in KILLZONES_UTC1.items():
        s = start[0] * 60 + start[1]
        e = end[0]   * 60 + end[1]
        if s <= t <= e:
            return name
    return ""

def killzone_badge(kz: str) -> str:
    if kz == "London":
        return "🟢 KZ London"
    if kz == "NY AM":
        return "🔵 KZ NY AM"
    if kz == "Asia":
        return "🟠 KZ Asia"
    return ""


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

    @staticmethod
    def rsi(series, period=14):
        delta = series.diff()
        gain  = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
        loss  = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def zlema(series, period=50, lag=17):
        src = series + (series - series.shift(lag))
        return src.ewm(span=period, adjust=False).mean()


# ----------------------------------------------------------------
#  MTF INSTITUTIONAL TREND  (miroir du Pine Script)
# ----------------------------------------------------------------
#  Weights : M=4.0  W=3.5  D=3.0  4H=2.5  1H=2.0  15m=1.5
#  Même logique que le tableau TradingView
# ----------------------------------------------------------------
TF_WEIGHTS = {
    "M":   4.0,
    "W":   3.5,
    "D":   3.0,
    "4H":  2.5,
    "1H":  2.0,
    "15m": 1.5,
}
TOTAL_WEIGHT = sum(TF_WEIGHTS.values())   # 17.0


def get_tf_trend(df: pd.DataFrame, tf_type: str):
    """
    Retourne (trend: int, strength: float, label: str)
    trend  : +1 Bull / -1 Bear / 0 Neutre
    strength : 0–100
    Miroir exact de getInstitutionalTrend() dans Pine Script.
    """
    if df is None or len(df) < 50:
        return 0, 40.0, "NEUT"

    close = df['close']

    # ── M & W : croisement EMA50 / SMA200 ─────────────────────
    if tf_type in ("M", "W"):
        if len(df) < 200:
            return 0, 40.0, "NEUT"
        sma200 = close.rolling(200).mean().iloc[-1]
        ema50  = close.ewm(span=50, adjust=False).mean().iloc[-1]
        if pd.isna(sma200) or pd.isna(ema50):
            return 0, 40.0, "NEUT"
        trend    = 1 if ema50 > sma200 else (-1 if ema50 < sma200 else 0)
        strength = 75.0 if ema50 != sma200 else 40.0
        label    = "BULL" if trend == 1 else ("BEAR" if trend == -1 else "NEUT")
        return trend, strength, label

    # ── 4H : 3 facteurs orthogonaux ───────────────────────────
    elif tf_type == "4H":
        score = 0
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        score += 1 if close.iloc[-1] > ema50 else -1

        adx_s, pdi_s, mdi_s = QuantEngine.adx(df, 14)
        score += 1 if pdi_s.iloc[-1] > mdi_s.iloc[-1] else -1

        # Daily open : première bougie du jour courant
        df_copy = df.copy()
        if df_copy.index.tz is None:
            df_copy.index = df_copy.index.tz_localize("UTC")
        today = df_copy.index[-1].date()
        day_rows = df_copy[df_copy.index.date == today]
        if not day_rows.empty:
            daily_open = day_rows['open'].iloc[0]
            score += 1 if close.iloc[-1] > daily_open else -1

        trend    = 1 if score > 0 else (-1 if score < 0 else 0)
        strength = 90.0 if abs(score) == 3 else (70.0 if abs(score) >= 1 else 40.0)
        label    = "BULL" if trend == 1 else ("BEAR" if trend == -1 else "NEUT")
        return trend, strength, label

    # ── H1 & 15m : confluence maximale ────────────────────────
    else:
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema9  = close.ewm(span=9,  adjust=False).mean()
        zl    = QuantEngine.zlema(close, 50, 17)
        rsi_v = QuantEngine.rsi(close, 14)
        macd  = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        sig   = macd.ewm(span=9, adjust=False).mean()

        c = close.iloc[-1]
        bullish = (c > zl.iloc[-1]    and
                   ema9.iloc[-1]  > ema21.iloc[-1] and
                   ema21.iloc[-1] > ema50.iloc[-1] and
                   rsi_v.iloc[-1] > 50             and
                   macd.iloc[-1]  > sig.iloc[-1])
        bearish = (c < zl.iloc[-1]    and
                   ema9.iloc[-1]  < ema21.iloc[-1] and
                   ema21.iloc[-1] < ema50.iloc[-1] and
                   rsi_v.iloc[-1] < 50             and
                   macd.iloc[-1]  < sig.iloc[-1])

        base_str  = min(80.0, abs(c - zl.iloc[-1]) / c * 1000)
        strength  = (base_str if (bullish or bearish) else 30.0)
        trend     = 1 if bullish else (-1 if bearish else 0)
        label     = "BULL" if trend == 1 else ("BEAR" if trend == -1 else "NEUT")
        return trend, strength, label


def compute_mtf_analysis(dfs: dict):
    """
    dfs = {"M": df_monthly, "W": df_weekly, "D": df_daily,
           "4H": df_4h, "1H": df_1h, "15m": df_15m}
    Retourne (alignment_pct: int, dominant: str, details: dict)
    Miroir exact du bloc MTF du Pine Script.
    """
    results = {}
    for tf, df in dfs.items():
        t, s, lbl = get_tf_trend(df, tf)
        results[tf] = {"trend": t, "strength": s, "label": lbl}

    # Macro filter (M → W → D → 4H)
    macro_trend = 0
    for tf in ("M", "W", "D", "4H"):
        if tf in results and results[tf]["trend"] != 0:
            macro_trend = results[tf]["trend"]
            break

    # Filtres 1H & 15m
    t1h  = results.get("1H",  {}).get("trend", 0)
    t15m = results.get("15m", {}).get("trend", 0)
    f1h  = 0 if (macro_trend != 0 and macro_trend != t1h)  else t1h
    f15m = 0 if (macro_trend != 0 and macro_trend != t15m) else t15m
    results["1H"]["trend"]  = f1h
    results["15m"]["trend"] = f15m

    bull_score = sum(
        TF_WEIGHTS[tf] for tf in TF_WEIGHTS
        if results.get(tf, {}).get("trend", 0) == 1
    )
    bear_score = sum(
        TF_WEIGHTS[tf] for tf in TF_WEIGHTS
        if results.get(tf, {}).get("trend", 0) == -1
    )

    alignment_pct = round(max(bull_score, bear_score) / TOTAL_WEIGHT * 100)
    dominant      = ("Bullish" if bull_score > bear_score
                     else "Bearish" if bear_score > bull_score
                     else "Neutral")

    return alignment_pct, dominant, results


# ----------------------------------------------------------------
#  DAILY BIAS AMÉLIORÉ  (3 facteurs — miroir Pine getDailyBias)
# ----------------------------------------------------------------
def get_daily_bias_v2(df_d: pd.DataFrame):
    """
    3 facteurs orthogonaux identiques au Pine Script :
    1. Prix vs EMA50 D1
    2. DI+ vs DI-
    3. Weekly Open
    Score ±3 → 90% | ±1/2 → 70% | 0 → 40%
    """
    if df_d is None or len(df_d) < 60:
        return "NEUTRAL", {}

    close = df_d['close']
    score = 0
    detail = {}

    # Facteur 1 : EMA 50
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    cur   = close.iloc[-1]
    f1    = "BULLISH" if cur > ema50 else "BEARISH"
    score += 1 if cur > ema50 else -1
    detail["EMA 50"] = f1

    # Facteur 2 : DI+ vs DI-
    _, pdi, mdi = QuantEngine.adx(df_d, 14)
    f2 = "BULLISH" if pdi.iloc[-1] > mdi.iloc[-1] else "BEARISH"
    score += 1 if pdi.iloc[-1] > mdi.iloc[-1] else -1
    detail["DI+/DI-"] = f2

    # Facteur 3 : Weekly Open
    df_copy = df_d.copy()
    if df_copy.index.tz is None:
        df_copy.index = df_copy.index.tz_localize("UTC")
    weekly_open_rows = df_copy[df_copy.index.dayofweek.isin([0, 6])]
    if not weekly_open_rows.empty:
        weekly_open = weekly_open_rows['open'].iloc[-1]
        f3 = "BULLISH" if cur > weekly_open else "BEARISH"
        score += 1 if cur > weekly_open else -1
        detail["Weekly Open"] = f3
    else:
        detail["Weekly Open"] = "NEUTRAL"

    detail["Score"] = score

    if   score ==  3: bias = "STRONG BULLISH"
    elif score >=  1: bias = "BULLISH"
    elif score == -3: bias = "STRONG BEARISH"
    elif score <= -1: bias = "BEARISH"
    else:             bias = "NEUTRAL"

    return bias, detail


# ----------------------------------------------------------------
#  SYSTÈME DE NOTATION  V11
# ----------------------------------------------------------------
def compute_score(flip_type, candles_ago,
                  mtf_pct, mtf_dominant,
                  zone_discount, zone_premium,
                  near_pdl, near_pdh, below_mid, above_mid,
                  in_bull_fvg, in_bear_fvg, fvg_near_bull, fvg_near_bear,
                  adx_val, pdi_val, mdi_val, atr_val, atr_mean):

    score        = 0
    score_detail = {}

    # ── TRIGGER HMA ──────────────────────────────────────────────
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

    signal_is_bull = (flip_type == "BULL")
    signal_is_bear = (flip_type == "BEAR")

    # ── MTF ALIGNMENT  (remplace Biais simple, 25 pts) ───────────
    # Vérifie que le MTF dominant est dans la même direction que le signal
    mtf_aligned = (
        (signal_is_bull and mtf_dominant == "Bullish") or
        (signal_is_bear and mtf_dominant == "Bearish")
    )
    if mtf_aligned:
        if   mtf_pct >= 80: pts = 25
        elif mtf_pct >= 65: pts = 18
        elif mtf_pct >= 50: pts = 10
        else:               pts = 5
    else:
        pts = 0   # contre-MTF = 0 pt (signal autorisé mais décoté)
    score += pts
    score_detail["MTF"] = pts

    # ── ZONE D'INTÉRÊT  (15 pts) ─────────────────────────────────
    if signal_is_bull:
        if zone_discount:   pts = 15
        elif below_mid:     pts = 8
        else:               pts = 0
    elif signal_is_bear:
        if zone_premium:    pts = 15
        elif above_mid:     pts = 8
        else:               pts = 0
    else:
        pts = 0
    score += pts
    score_detail["Zone"] = pts

    # ── FVG M15  (15 pts) ─────────────────────────────────────────
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

    # ── ADX / MOMENTUM  (10 pts) ──────────────────────────────────
    adx_dir_ok = (
        (signal_is_bull and pdi_val > mdi_val) or
        (signal_is_bear and mdi_val > pdi_val)
    )
    if   adx_val > 25 and adx_dir_ok: pts = 10
    elif adx_val > 20 and adx_dir_ok: pts = 6
    elif adx_val > 20:                pts = 3
    else:                             pts = 0
    score += pts
    score_detail["ADX"] = pts

    # ── ATR  (5 pts) ──────────────────────────────────────────────
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


# ----------------------------------------------------------------
#  FLIP HMA
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

    return (in_bull, in_bear,
            bull_fvgs[0] if bull_fvgs else None,
            bear_fvgs[0] if bear_fvgs else None)


# ----------------------------------------------------------------
#  FETCH OANDA
# ----------------------------------------------------------------
MAX_RETRIES = 3
RETRY_DELAY = 1.5

def fetch_oanda_data(client, instrument, granularity, count):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = instruments.InstrumentsCandles(
                instrument=instrument,
                params={"count": count, "granularity": granularity}
            )
            client.request(r)

            if not isinstance(r.response, dict) or "candles" not in r.response:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
                return pd.DataFrame(), "Réponse inattendue"

            rows = [
                {"time":  pd.to_datetime(c["time"]),
                 "open":  float(c["mid"]["o"]),
                 "high":  float(c["mid"]["h"]),
                 "low":   float(c["mid"]["l"]),
                 "close": float(c["mid"]["c"])}
                for c in r.response["candles"] if c.get("complete")
            ]

            if not rows:
                return pd.DataFrame(), "Aucune bougie complète"

            df = pd.DataFrame(rows).set_index("time")
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            return df, None

        except Exception as e:
            err_str = str(e)
            if "401" in err_str or "Unauthorized" in err_str:
                return pd.DataFrame(), "TOKEN_INVALID"
            elif "403" in err_str:
                return pd.DataFrame(), "TOKEN_FORBIDDEN"
            elif "429" in err_str:
                time.sleep(RETRY_DELAY * attempt * 3)
            elif "500" in err_str or "503" in err_str:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
            if attempt == MAX_RETRIES:
                return pd.DataFrame(), str(e)

    return pd.DataFrame(), "MAX_RETRIES"


# ----------------------------------------------------------------
#  TEST CONNEXION
# ----------------------------------------------------------------
def test_oanda_connection(client, account_id=None):
    try:
        if account_id:
            r = accounts.AccountDetails(account_id)
        else:
            r = accounts.AccountList()
        client.request(r)
        if account_id:
            acc      = r.response.get("account", {})
            currency = acc.get("currency", "?")
            balance  = acc.get("balance", "?")
            return True, f"✅ Connecté — compte `{account_id}` · {currency} {balance}"
        else:
            accs = r.response.get("accounts", [])
            ids  = [a.get("id","?") for a in accs]
            return True, f"✅ Connecté — {len(ids)} compte(s)"
    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err:
            return False, "❌ Token invalide ou expiré"
        return False, f"❌ Erreur : {err}"


# ----------------------------------------------------------------
#  ANALYSE PRINCIPALE
# ----------------------------------------------------------------
def analyze_asset(client, ticker, freshness_limit_min=30, debug_log=None):

    def _reject(reason):
        if debug_log is not None:
            debug_log.append((ticker, reason))
        return None

    try:
        # ── FETCH  ──────────────────────────────────────────────
        df_m15, err_m15 = fetch_oanda_data(client, ticker, "M15", 400)
        df_h1,  err_h1  = fetch_oanda_data(client, ticker, "H1",  200)
        df_d,   err_d   = fetch_oanda_data(client, ticker, "D",   300)
        df_4h,  _       = fetch_oanda_data(client, ticker, "H4",  300)
        df_w,   _       = fetch_oanda_data(client, ticker, "W",   100)
        df_mo,  _       = fetch_oanda_data(client, ticker, "M",   60)

        if df_m15.empty:
            return _reject(f"FETCH_M15: {err_m15}")
        if df_d.empty:
            return _reject(f"FETCH_D: {err_d}")

        price = df_m15['close'].iloc[-1]

        # ── DAILY BIAS V2 (3 facteurs Pine) ─────────────────────
        bias, bias_detail = get_daily_bias_v2(df_d)
        bias_bull = bias in ("BULLISH", "STRONG BULLISH")
        bias_bear = bias in ("BEARISH", "STRONG BEARISH")

        # ── MTF ANALYSIS ─────────────────────────────────────────
        dfs_mtf = {
            "M":   df_mo  if not (df_mo  is None or (hasattr(df_mo,  'empty') and df_mo.empty))  else None,
            "W":   df_w   if not (df_w   is None or (hasattr(df_w,   'empty') and df_w.empty))   else None,
            "D":   df_d   if not df_d.empty  else None,
            "4H":  df_4h  if not (df_4h  is None or (hasattr(df_4h,  'empty') and df_4h.empty))  else None,
            "1H":  df_h1  if not (df_h1  is None or (hasattr(df_h1,  'empty') and df_h1.empty))  else None,
            "15m": df_m15 if not df_m15.empty else None,
        }
        mtf_pct, mtf_dominant, mtf_details = compute_mtf_analysis(dfs_mtf)

        # ── HMA 20 FLIP M15 ──────────────────────────────────────
        hma = QuantEngine.hma(df_m15['close'], 20)
        if hma.isna().iloc[-5:].any():
            return _reject("HMA_NAN")

        flip_type, candles_ago = find_last_hma_flip(hma, max_lookback=10)
        if flip_type is None:
            return _reject("NO_HMA_FLIP")

        # Alignement biais / flip
        if   (flip_type == "BULL" and bias_bull) or (flip_type == "BEAR" and bias_bear):
            bias_alignment = "ALIGNED"
        elif bias == "NEUTRAL":
            bias_alignment = "NEUTRAL"
        else:
            bias_alignment = "COUNTER"

        mins_ago      = candles_ago * 15
        freshness_str = f"⚡ {mins_ago} min" if mins_ago <= freshness_limit_min else f"⏳ {mins_ago} min"
        signal_fresh  = mins_ago <= freshness_limit_min

        # ── GATE : PRIX DU BON CÔTÉ DE LA HMA ───────────────────
        hma_now = hma.iloc[-1]
        if flip_type == "BULL" and price <= hma_now:
            return _reject("PRICE_BELOW_HMA_ON_BULL")
        if flip_type == "BEAR" and price >= hma_now:
            return _reject("PRICE_ABOVE_HMA_ON_BEAR")

        # ── FVG ───────────────────────────────────────────────────
        atr_m15  = QuantEngine.atr(df_m15, 14)
        atr_val  = atr_m15.iloc[-1]
        atr_mean = atr_m15.iloc[-50:].mean()

        in_bull_fvg, in_bear_fvg, nb_fvg, nr_fvg = detect_fvg(df_m15, price, lookback=80)

        fvg_near_bull = (nb_fvg is not None
                         and abs(price - (nb_fvg[0] + nb_fvg[1]) / 2) < atr_val * 2)
        fvg_near_bear = (nr_fvg is not None
                         and abs(price - (nr_fvg[0] + nr_fvg[1]) / 2) < atr_val * 2)

        if flip_type == "BULL" and not (in_bull_fvg or fvg_near_bull):
            return _reject("NO_BULL_FVG")
        if flip_type == "BEAR" and not (in_bear_fvg or fvg_near_bear):
            return _reject("NO_BEAR_FVG")

        # ── ZONE PDH/PDL ─────────────────────────────────────────
        pdh = df_d['high'].iloc[-2]
        pdl = df_d['low'].iloc[-2]

        ny_tz     = pytz.timezone('America/New_York')
        df_ny     = df_m15.copy()
        df_ny.index = df_ny.index.tz_convert(ny_tz)
        today_ny  = datetime.now(ny_tz).date()
        mask      = ((df_ny.index.date == today_ny) &
                     (df_ny.index.hour == 0) & (df_ny.index.minute == 0))
        mid_c     = df_ny[mask]
        m_open    = mid_c['open'].iloc[0] if not mid_c.empty else df_ny['open'].iloc[0]

        atr_d         = QuantEngine.atr(df_d, 14).iloc[-1]
        below_mid     = price < m_open
        above_mid     = price > m_open
        near_pdl      = price <= (pdl + atr_d)
        near_pdh      = price >= (pdh - atr_d)
        zone_discount = below_mid and near_pdl
        zone_premium  = above_mid and near_pdh
        zone_label    = ("DISCOUNT" if zone_discount else
                         "PREMIUM"  if zone_premium  else "NEUTRE")

        # ── ADX H1 ───────────────────────────────────────────────
        df_adx_src          = df_h1 if not (df_h1 is None or (hasattr(df_h1,'empty') and df_h1.empty)) and len(df_h1) >= 20 else df_m15
        adx_s, pdi_s, mdi_s = QuantEngine.adx(df_adx_src, 14)
        adx_val = round(adx_s.iloc[-1], 1)
        pdi_val = round(pdi_s.iloc[-1], 1)
        mdi_val = round(mdi_s.iloc[-1], 1)

        # ── SCORE ─────────────────────────────────────────────────
        score, grade, score_detail = compute_score(
            flip_type, candles_ago,
            mtf_pct, mtf_dominant,
            zone_discount, zone_premium,
            near_pdl, near_pdh, below_mid, above_mid,
            in_bull_fvg, in_bear_fvg,
            fvg_near_bull, fvg_near_bear,
            adx_val, pdi_val, mdi_val,
            atr_val, atr_mean
        )

        sig = (("▲ LONG"  if flip_type == "BULL" else "▼ SHORT")
               if signal_fresh else
               ("LONG (expiré)" if flip_type == "BULL" else "SHORT (expiré)"))

        # MTF label compact : "78% Bull" 
        mtf_label = f"{mtf_pct}% {mtf_dominant[:4]}"

        return {
            "Actif + Note":  ticker,
            "Signal":        sig,
            "Fraîcheur":     freshness_str,
            "Score /100":    score,
            "Grade":         grade,
            "Biais Daily":   bias,
            "Alignement":    bias_alignment,
            "Zone":          zone_label,
            "ADX H1":        adx_val,
            "MTF %":         mtf_label,
            "MTF Pct":       mtf_pct,
            "MTF Dom":       mtf_dominant,
            "MTF Details":   mtf_details,
            "Score Detail":  score_detail,
        }

    except Exception as e:
        if debug_log is not None:
            debug_log.append((ticker, f"EXCEPTION: {e}"))
        return None


# ----------------------------------------------------------------
#  INTERFACE STREAMLIT
# ----------------------------------------------------------------
def main():
    st.set_page_config(
        page_title="BLUESTAR SNIPER V11",
        layout="wide",
        initial_sidebar_state="collapsed"
    )

    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Space+Grotesk:wght@400;600;700&display=swap');

    html, body, [class*="css"] { font-family: 'Space Grotesk', sans-serif; }
    .stApp { background: #0e0e14; }
    h1 { font-family: 'IBM Plex Mono', monospace !important; letter-spacing: .04em; }

    .stButton > button {
        background: #141420 !important; color: #a0b4cc !important;
        border: 1px solid #2a3448 !important;
        font-family: 'IBM Plex Mono', monospace !important;
        font-weight: 700 !important; letter-spacing: .06em !important;
        border-radius: 4px !important; transition: all .2s ease !important;
    }
    .stButton > button:hover {
        background: #1c2030 !important; border-color: #3a4a68 !important;
        color: #c8d8e8 !important;
    }
    .stSelectbox label { color: #606080 !important; font-size: 12px !important;
        text-transform: uppercase !important; letter-spacing: .08em !important; }
    [data-testid="stMetric"] {
        background: #121218; border: 1px solid #1e1e2e;
        border-radius: 6px; padding: 12px 16px !important;
    }
    [data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace !important; }
    .streamlit-expanderHeader { color: #606080 !important; font-size: 12px !important;
        text-transform: uppercase !important; }
    hr { border-color: #1a1a28 !important; }
    </style>
    """, unsafe_allow_html=True)

    # ── HEADER ────────────────────────────────────────────────────
    kz_now  = get_current_killzone()
    kz_html = (f'<span style="font-size:12px;font-family:\'IBM Plex Mono\',monospace;'
               f'color:#5a9e7a;letter-spacing:.08em;margin-left:14px">'
               f'{killzone_badge(kz_now)}</span>'
               if kz_now else "")

    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:4px">
      <span style="font-size:36px">🎯</span>
      <div>
        <h1 style="margin:0;font-size:28px;color:#e8e8f8">
          BLUESTAR SNIPER V11 {kz_html}
        </h1>
        <p style="margin:0;color:#4a4a7a;font-family:'IBM Plex Mono',monospace;font-size:12px;letter-spacing:.1em">
          HMA 20 M15 · MTF INSTITUTIONAL · KILLZONE · SCORE /100 · ICT / SMC ENGINE
        </p>
      </div>
    </div>
    <hr>
    """, unsafe_allow_html=True)

    # ── SECRETS ───────────────────────────────────────────────────
    missing = [k for k in ("OANDA_ACCESS_TOKEN", "OANDA_ACCOUNT_ID") if k not in st.secrets]
    if missing:
        st.error(f"🔑 **Secret(s) manquant(s) :** `{'`, `'.join(missing)}`")
        st.stop()

    ACCESS_TOKEN = st.secrets["OANDA_ACCESS_TOKEN"]
    ACCOUNT_ID   = st.secrets["OANDA_ACCOUNT_ID"]
    env          = "practice"

    # ── CONTRÔLES ─────────────────────────────────────────────────
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        freshness  = st.selectbox("Fraîcheur max du signal (min)", [15, 30, 45, 60], index=1)
    with col2:
        min_grade  = st.selectbox("Grade minimum", ["Tous", "B", "B+", "A", "A+"], index=0)
    with col3:
        show_debug = st.toggle("🐛 Debug", value=False)

    with st.expander("📘 Grille de notation V11"):
        st.markdown("""
| Critère | Max | Détail |
|---|---|---|
| **Trigger HMA flip** | 30 pts | ≤15 min = 30 · ≤30 min = 20 · ≤45 min = 10 · >45 min = 0 |
| **MTF Alignment** | 25 pts | ≥80% aligné = 25 · ≥65% = 18 · ≥50% = 10 · contre-MTF = 0 |
| **Zone d'intérêt** | 15 pts | Discount+PDL ou Premium+PDH = 15 · Zone seule = 8 · Hors zone = 0 |
| **FVG M15** | 15 pts | Dans le FVG = 15 · FVG proche = 7 · Absent = 0 |
| **ADX momentum** | 10 pts | ADX>25+DI ok = 10 · ADX>20+DI ok = 6 · ADX>20 seul = 3 |
| **ATR actif** | 5 pts | ≥ moyenne = 5 · ≥ 50% = 3 · Plat = 0 |

| Grade | Score | Signal |
|---|---|---|
| 💎 **A+** | 85-100 | MTF aligné + HMA frais + zone + FVG = trader |
| 🥇 **A** | 70-84 | Très bon — 1 élément mineur manque |
| 🥈 **B+** | 55-69 | En formation — surveiller |
| 🔵 **B** | 40-54 | Partiel — attendre confirmation |
| ⚪ **C** | < 40 | Faible — ignorer |

**MTF Weights :** M=4.0 · W=3.5 · D=3.0 · 4H=2.5 · 1H=2.0 · 15m=1.5
        """)

    # ── LANCER ────────────────────────────────────────────────────
    run = st.button("🚀  LANCER LE SCANNER", use_container_width=True)

    if not run:
        st.markdown("""
        <div style="text-align:center;padding:60px 0;color:#2a2a4a">
          <div style="font-size:48px">📡</div>
          <div style="font-family:'IBM Plex Mono',monospace;font-size:14px;letter-spacing:.1em;margin-top:12px">
            EN ATTENTE — APPUIE SUR LANCER
          </div>
        </div>
        """, unsafe_allow_html=True)
        return

    client = oandapyV20.API(access_token=ACCESS_TOKEN, environment=env)

    assets = [
        "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
        "AUD_USD", "USD_CAD", "NZD_USD",
        "EUR_GBP", "EUR_JPY", "EUR_CHF",
        "EUR_AUD", "EUR_CAD", "EUR_NZD",
        "GBP_JPY", "GBP_CHF", "GBP_AUD",
        "GBP_CAD", "GBP_NZD",
        "AUD_JPY", "CAD_JPY", "CHF_JPY", "NZD_JPY",
        "AUD_CAD", "AUD_CHF", "AUD_NZD",
        "CAD_CHF", "NZD_CAD", "NZD_CHF",
        "XAU_USD", "XAG_USD",
        "US30_USD", "NAS100_USD", "DE30_EUR",
    ]

    results   = []
    debug_log = []
    progress  = st.progress(0)
    status    = st.empty()
    t_start   = time.time()

    with st.spinner("Analyse MTF en cours…"):
        for i, ticker in enumerate(assets):
            status.caption(f"⏳ Analyse MTF {ticker}… ({i+1}/{len(assets)})")
            res = analyze_asset(client, ticker,
                                freshness_limit_min=freshness,
                                debug_log=debug_log)
            if res:
                results.append(res)
            time.sleep(0.15)
            progress.progress((i + 1) / len(assets))

    elapsed = round(time.time() - t_start, 1)
    status.empty()
    progress.empty()

    # ── DEBUG ─────────────────────────────────────────────────────
    if show_debug:
        with st.expander(f"🐛 Debug — {len(debug_log)} rejetés en {elapsed}s"):
            if debug_log:
                cols_d = st.columns(2)
                with cols_d[0]:
                    cats = Counter()
                    for _, reason in debug_log:
                        cat = reason.split("_")[0] if "_" in reason else reason[:20]
                        cats[cat] += 1
                    st.markdown("**Raisons agrégées**")
                    for cat, n in cats.most_common():
                        st.markdown(f"- `{cat}` → **{n}**")
                with cols_d[1]:
                    st.markdown("**Détail par actif**")
                    for ticker, reason in debug_log[:30]:
                        st.markdown(f"`{ticker}` — {reason}")

    if not results:
        st.warning("**Aucun signal valide détecté** sur cette session.")
        return

    df = pd.DataFrame(results)

    # ── FILTRE GRADE ──────────────────────────────────────────────
    grade_order = {"A+": 5, "A": 4, "B+": 3, "B": 2, "C": 1}
    min_map     = {"Tous": 0, "B": 2, "B+": 3, "A": 4, "A+": 5}
    min_val     = min_map[min_grade]
    df = df[df["Grade"].map(grade_order) >= min_val]

    if df.empty:
        st.info("Aucun signal avec ces filtres — élargis les critères.")
        return

    # ── TRI ───────────────────────────────────────────────────────
    def sort_key(row):
        fresh = 1 if "⚡" in str(row["Fraîcheur"]) else 0
        g     = grade_order.get(row["Grade"], 0)
        s     = row["Score /100"]
        m     = row["MTF Pct"]
        return (-fresh, -g, -s, -m)

    df["_sk"] = df.apply(sort_key, axis=1)
    df = df.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)

    # ── MÉTRIQUES RAPIDES ─────────────────────────────────────────
    a_plus   = len(df[(df["Grade"] == "A+") & df["Fraîcheur"].str.contains("⚡", na=False)])
    a_grade  = len(df[(df["Grade"] == "A")  & df["Fraîcheur"].str.contains("⚡", na=False)])
    b_watch  = len(df[df["Grade"].isin(["B+","B"]) & df["Fraîcheur"].str.contains("⚡", na=False)])
    longs    = len(df[df["Signal"].str.contains("LONG",  na=False) & ~df["Signal"].str.contains("expiré")])
    shorts   = len(df[df["Signal"].str.contains("SHORT", na=False) & ~df["Signal"].str.contains("expiré")])

    mc = st.columns(5)
    with mc[0]: st.metric("💎 Signaux A+", a_plus)
    with mc[1]: st.metric("🥇 Signaux A",  a_grade)
    with mc[2]: st.metric("👀 Watch B/B+", b_watch)
    with mc[3]: st.metric("▲ LONG actifs", longs)
    with mc[4]: st.metric("▼ SHORT actifs", shorts)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── TABLE HTML ────────────────────────────────────────────────
    GRADE_STYLE = {
        "A+": {"color": "#5a9e7a", "bg": "rgba(90,158,122,0.07)", "label": "A+"},
        "A":  {"color": "#4e8a6c", "bg": "rgba(78,138,108,0.05)", "label": "A"},
        "B+": {"color": "#9a7820", "bg": "transparent",           "label": "B+"},
        "B":  {"color": "#5a6880", "bg": "transparent",           "label": "B"},
        "C":  {"color": "#383848", "bg": "transparent",           "label": "C"},
    }

    def sig_style(s):
        if "LONG"  in s and "expiré" not in s:
            return "color:#5a9e7a;font-weight:700;font-size:16px;letter-spacing:.03em"
        if "SHORT" in s and "expiré" not in s:
            return "color:#9e4a3a;font-weight:700;font-size:16px;letter-spacing:.03em"
        return "color:#303040;font-size:12px"

    def adx_style(v):
        try:
            f = float(v)
            if f >= 25: return "color:#5a9e7a;font-weight:700"
            if f >= 20: return "color:#9a7820;font-weight:700"
            return "color:#404055"
        except: return "color:#333"

    def bias_daily_style(b):
        if "STRONG BULLISH" in b: return "color:#39FF14;font-weight:700;font-size:14px"
        if "BULLISH"        in b: return "color:#5a9e7a;font-weight:700;font-size:14px"
        if "STRONG BEARISH" in b: return "color:#FF003C;font-weight:700;font-size:14px"
        if "BEARISH"        in b: return "color:#9e4a3a;font-weight:700;font-size:14px"
        return "color:#404055;font-size:14px"

    def bias_daily_label(b):
        if b == "STRONG BULLISH": return "▲▲ STRONG BULL"
        if b == "BULLISH":        return "▲ BULL"
        if b == "STRONG BEARISH": return "▼▼ STRONG BEAR"
        if b == "BEARISH":        return "▼ BEAR"
        return "— NEUTRAL"

    def zone_style(z):
        if "DISCOUNT" in z: return "color:#4a7898;font-weight:600"
        if "PREMIUM"  in z: return "color:#8a6028;font-weight:600"
        return "color:#404055"

    def fresh_style(f):
        return "color:#9a7820;font-weight:700" if "⚡" in f else "color:#303040"

    def mtf_style(pct, dominant):
        bull = dominant == "Bullish"
        bear = dominant == "Bearish"
        if pct >= 70:
            c = "#5a9e7a" if bull else ("#9e4a3a" if bear else "#606080")
            return f"color:{c};font-weight:700"
        if pct >= 50:
            c = "#4a8060" if bull else ("#7e3a2a" if bear else "#505060")
            return f"color:{c};font-weight:600"
        return "color:#404055"

    def score_bar(score):
        color = "#5a9e7a" if score >= 70 else "#9a7820" if score >= 40 else "#9e4a3a"
        width = max(4, score)
        return f"""<div style="display:flex;align-items:center;gap:8px">
          <div style="width:60px;height:4px;background:#1a1a22;border-radius:2px;overflow:hidden">
            <div style="width:{width}%;height:100%;background:{color};border-radius:2px"></div>
          </div>
          <span style="color:{color};font-weight:700;font-size:15px">{score}</span>
        </div>"""

    def mtf_bar(pct, dominant):
        bull = dominant == "Bullish"
        bear = dominant == "Bearish"
        bar_c = "#5a9e7a" if bull else ("#9e4a3a" if bear else "#606080")
        txt_c = bar_c
        return f"""<div style="display:flex;align-items:center;gap:8px">
          <div style="width:44px;height:4px;background:#1a1a22;border-radius:2px;overflow:hidden">
            <div style="width:{pct}%;height:100%;background:{bar_c};border-radius:2px"></div>
          </div>
          <span style="color:{txt_c};font-weight:700;font-size:12px">{pct}%</span>
        </div>"""

    html = """
<style>
.sc-wrap{overflow-x:auto;margin-top:4px}
.sc-tbl{width:100%;border-collapse:collapse;font-family:'IBM Plex Mono','Courier New',monospace;font-size:15px}
.sc-tbl thead tr{border-bottom:2px solid #1e1e3a}
.sc-tbl th{
  padding:10px 16px;text-align:left;color:#3a3a6a;
  font-size:12px;text-transform:uppercase;letter-spacing:.12em;font-weight:600;white-space:nowrap
}
.sc-tbl td{padding:13px 16px;border-bottom:1px solid #0f0f1e;vertical-align:middle}
.sc-tbl tr:hover td{background:#111118 !important}
.ticker{font-size:22px;font-weight:800;color:#c8c8d8;letter-spacing:.04em;white-space:nowrap;font-family:'IBM Plex Mono',monospace}
.bias-tag{font-size:12px;font-weight:700;letter-spacing:.07em;vertical-align:middle;margin-left:8px;text-transform:uppercase;opacity:.85}
.grade-pill{
  display:inline-block;padding:2px 8px;border-radius:2px;
  font-size:12px;font-weight:700;letter-spacing:.08em;
  border:1px solid currentColor;margin-left:10px;vertical-align:middle;font-family:'IBM Plex Mono',monospace
}
.kz-badge{
  display:inline-block;padding:2px 7px;border-radius:2px;
  font-size:10px;font-weight:700;letter-spacing:.06em;
  background:rgba(90,158,122,0.12);color:#5a9e7a;
  border:1px solid rgba(90,158,122,0.3);margin-left:8px;vertical-align:middle
}
.mtf-row{display:flex;flex-direction:column;gap:3px;min-width:80px}
.mtf-tf{font-size:10px;color:#3a3a5a;letter-spacing:.06em}
</style>
<div class="sc-wrap"><table class="sc-tbl">
<thead><tr>
  <th style="width:100px">Fraîcheur</th>
  <th>Actif</th>
  <th>Signal</th>
  <th>Biais Daily</th>
  <th>Zone</th>
  <th>Score /100</th>
  <th>ADX H1</th>
</tr></thead><tbody>
"""
    active_kz = get_current_killzone()

    for _, row in df.iterrows():
        grade     = str(row.get("Grade","C"))
        fresh     = "⚡" in str(row.get("Fraîcheur",""))
        gs        = GRADE_STYLE.get(grade, GRADE_STYLE["C"])
        score     = int(row.get("Score /100", 0))
        ticker    = str(row.get("Actif + Note","")).split("  ")[0].strip()
        sig       = str(row.get("Signal","—"))
        adx_v     = str(row.get("ADX H1","—"))
        bias      = str(row.get("Biais Daily","—"))
        alignment = str(row.get("Alignement","ALIGNED"))
        zone      = str(row.get("Zone","—"))
        fresh_str = str(row.get("Fraîcheur","—"))
        bull_bias  = "BULLISH" in bias
        bias_icon  = "▲" if bull_bias else "▼"
        bias_lbl   = ("STRONG BULL" if bias == "STRONG BULLISH"
                      else "BULL"        if bias == "BULLISH"
                      else "STRONG BEAR" if bias == "STRONG BEARISH"
                      else "BEAR"        if "BEAR" in bias
                      else "NEUTRAL")
        bias_color = "#5a9e7a" if bull_bias else ("#9e4a3a" if "BEAR" in bias else "#606080")

        align_tag = ""
        if alignment == "COUNTER":
            align_tag = '<span style="font-size:10px;color:#9e4a3a;font-weight:700;margin-left:6px;letter-spacing:.05em">⚠ COUNTER</span>'
        elif alignment == "NEUTRAL":
            align_tag = '<span style="font-size:10px;color:#606080;font-weight:600;margin-left:6px;letter-spacing:.05em">~ NEUTRAL</span>'

        # Badge killzone sur le signal
        kz_tag = f'<span class="kz-badge">{killzone_badge(active_kz)}</span>' if active_kz and fresh else ""

        row_bg = gs["bg"] if fresh and grade in ("A+","A") else "transparent"

        html += f"""<tr style="background:{row_bg}">
  <td style="{fresh_style(fresh_str)}">{fresh_str}</td>
  <td style="white-space:nowrap">
    <span class="ticker">{ticker}</span>
    <span class="bias-tag" style="color:{bias_color}">{bias_icon} {bias_lbl}</span>
    <span class="grade-pill" style="color:{gs['color']}">{gs['label']}</span>
    {align_tag}
  </td>
  <td style="{sig_style(sig)}">{sig}{kz_tag}</td>
  <td style="{bias_daily_style(bias)}">{bias_daily_label(bias)}</td>
  <td style="{zone_style(zone)}">{zone}</td>
  <td>{score_bar(score)}</td>
  <td style="{adx_style(adx_v)}">{adx_v}</td>
</tr>"""

    html += "</tbody></table></div>"
    st.markdown(html, unsafe_allow_html=True)

    st.markdown(f"""
    <div style="color:#2a2a4a;font-family:'IBM Plex Mono',monospace;font-size:10px;
                text-align:right;margin-top:6px;letter-spacing:.06em">
      {len(df)} signal(s) · scanné en {elapsed}s · {datetime.now().strftime('%H:%M:%S')}
      {'· <span style="color:#5a9e7a">KZ ' + active_kz + ' ACTIVE</span>' if active_kz else ''}
    </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
