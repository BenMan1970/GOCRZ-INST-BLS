import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.accounts as accounts
from datetime import datetime
import pytz
import time
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

# ================================================================
#  BLUESTAR SNIPER V16  —  ICT SIGNAL ENGINE  (AUDIT-FIXED)
#  Corrections appliquées :
#  ✅  BUG-001  PDH/PDL : _d1_ref = -1 (J-1, pas J-2)
#  ✅  BUG-002  MTF alignment calculé AVANT mutation 1H/15m
#  ✅  BUG-003  except:pass remplacés par logs explicites
#  ✅  BUG-004  ADX : exclusion mutuelle DM via np.where (Wilder)
#  ✅  BUG-005  Client OANDA créé par thread (thread-safety)
#  ✅  BUG-006  HMA : suppression du .ewm(5) non standard
#  ✅  BUG-007  get_tf_trend 4H : filtrage temporel corrigé
#  ✅  BUG-009  Swing points : comparaison float avec tolérance
#  ✅  BUG-010  midnight_open : validation de l'heure exacte
#  ✅  BUG-013  Staleness check sur df_m15 (données stale)
#  ✅  BUG-014  find_last_hma_flip : candles_ago sécurisé
#  ✅  BUG-015  Weekly Open : lundi uniquement, semaine courante
#  ✅  BUG-016  RSI : loss=0 → RSI=100, min_periods correct
#  ✅  BUG-017  Boucle asset parallélisée (ThreadPoolExecutor)
#  ✅  BUG-018  Killzones en UTC fixe (DST-proof)
#  ✅  BUG-019  Retry 429 : continue ajouté
#  ✅  BUG-020  OANDA_ENVIRONMENT via st.secrets
#  ✅  BUG-021  score_bar : score négatif affiché correctement
#  ✅  BUG-022  is_valid_df() helper, duplication supprimée
#  ✅  Dead code near_pdl/near_pdh supprimé de compute_score
# ================================================================

logger = logging.getLogger("bluestar_sniper")
logging.basicConfig(level=logging.WARNING)

# ----------------------------------------------------------------
#  HELPER  (BUG-022)
# ----------------------------------------------------------------
def is_valid_df(df, min_rows: int = 1) -> bool:
    """Retourne True si df est un DataFrame non-vide avec au moins min_rows lignes."""
    return df is not None and isinstance(df, pd.DataFrame) and len(df) >= min_rows


# ----------------------------------------------------------------
#  KILLZONE DETECTOR  —  UTC fixe, DST-proof  (BUG-018)
# ----------------------------------------------------------------
# Plages en UTC fixe :
#   London  = 07:00-08:30 UTC  (03:00-04:30 ET)
#   NY AM   = 13:30-15:00 UTC  (09:30-11:00 ET)
#   Asia    = 00:00-02:00 UTC
KILLZONES_UTC = {
    "London": ((7,  0), (8,  30)),
    "NY AM":  ((13, 30), (15, 0)),
    "Asia":   ((0,  0),  (2,  0)),
}

INDICES = {"US30_USD", "NAS100_USD", "DE30_EUR"}

def get_current_killzone() -> str:
    now_utc = datetime.now(pytz.UTC)
    t = now_utc.hour * 60 + now_utc.minute
    for name, (start, end) in KILLZONES_UTC.items():
        s = start[0] * 60 + start[1]
        e = end[0]   * 60 + end[1]
        if s <= t <= e:
            return name
    return ""

def killzone_badge(kz: str) -> str:
    if kz == "London": return "🟢 KZ London"
    if kz == "NY AM":  return "🔵 KZ NY AM"
    if kz == "Asia":   return "🟠 KZ Asia"
    return ""


# ----------------------------------------------------------------
#  QUANT ENGINE
# ----------------------------------------------------------------
class QuantEngine:

    @staticmethod
    def wma(series, period):
        weights = np.arange(1, period + 1, dtype=np.float64)
        w_sum   = weights.sum()
        arr     = series.to_numpy(dtype=np.float64)
        n       = len(arr)
        out     = np.full(n, np.nan)
        for i in range(period - 1, n):
            out[i] = np.dot(arr[i - period + 1: i + 1], weights) / w_sum
        return pd.Series(out, index=series.index)

    @staticmethod
    def hma(series, period=20):
        """HMA standard : WMA(2·WMA(n/2) - WMA(n), √n)  — sans EWM parasite (BUG-006)."""
        half   = int(period / 2)
        sqrt_p = int(np.sqrt(period))
        raw    = 2 * QuantEngine.wma(series, half) - QuantEngine.wma(series, period)
        if raw.isna().all():
            return pd.Series(np.nan, index=series.index)
        return QuantEngine.wma(raw, sqrt_p)          # ← EWM(5) non standard supprimé

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
        """ADX Wilder avec exclusion mutuelle stricte des DM via np.where (BUG-004)."""
        high      = df['high']
        low       = df['low']
        prev_high = high.shift(1)
        prev_low  = low.shift(1)

        up_move   = high - prev_high
        down_move = prev_low - low

        pdm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=df.index, dtype=np.float64
        )
        mdm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=df.index, dtype=np.float64
        )

        atr_v  = QuantEngine.atr(df, period)
        pdm_s  = pdm.ewm(alpha=1 / period, adjust=False).mean()
        mdm_s  = mdm.ewm(alpha=1 / period, adjust=False).mean()

        safe_atr = atr_v.replace(0, np.nan)
        pdi = 100 * pdm_s / safe_atr
        mdi = 100 * mdm_s / safe_atr
        dx  = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
        adx = dx.ewm(alpha=1 / period, adjust=False).mean()
        return adx, pdi, mdi

    @staticmethod
    def rsi(series, period=14):
        """RSI Wilder : gestion loss=0 → RSI=100 ; min_periods correct (BUG-016)."""
        delta    = series.diff()
        gain     = delta.where(delta > 0, 0.0)
        loss     = (-delta.where(delta < 0, 0.0))

        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

        rsi_out              = pd.Series(np.nan, index=series.index, dtype=float)
        mask_zero            = avg_loss == 0
        rsi_out[mask_zero]   = 100.0
        mask_nz              = ~mask_zero & avg_loss.notna() & avg_gain.notna()
        rsi_out[mask_nz]     = 100 - (100 / (1 + avg_gain[mask_nz] / avg_loss[mask_nz]))
        return rsi_out

    @staticmethod
    def zlema(series, period=50, lag=17):
        src = series + (series - series.shift(lag))
        return src.ewm(span=period, adjust=False).mean()


# ----------------------------------------------------------------
#  ADR — Average Daily Range (forex/metals uniquement)
# ----------------------------------------------------------------
def compute_adr(df_d: pd.DataFrame, period: int = 14) -> float:
    if not is_valid_df(df_d, 2):
        return float("nan")
    ranges = (df_d["high"] - df_d["low"]).iloc[-(period + 1):-1]
    return float(ranges.mean()) if not ranges.empty else float("nan")


# ----------------------------------------------------------------
#  ADR CONSUMED  (V16) — range consommé depuis minuit NY
# ----------------------------------------------------------------
def compute_adr_consumed(df_m15: pd.DataFrame, midnight_open_price: float,
                         adr_value: float) -> float | None:
    if not is_valid_df(df_m15) or adr_value is None or adr_value <= 0:
        return None
    try:
        ny_tz    = pytz.timezone("America/New_York")
        idx      = df_m15.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        idx_ny   = idx.tz_convert(ny_tz)
        today_ny = datetime.now(ny_tz).date()

        mask       = idx_ny.date == today_ny
        today_bars = df_m15[mask]
        if today_bars.empty:
            return None

        today_h  = float(today_bars["high"].max())
        today_l  = float(today_bars["low"].min())
        consumed = (today_h - today_l) / adr_value * 100.0
        return round(min(consumed, 100.0), 1)
    except Exception as _e:
        logger.warning("compute_adr_consumed error: %s", _e)
        return None


# ----------------------------------------------------------------
#  MTF INSTITUTIONAL TREND
# ----------------------------------------------------------------
TF_WEIGHTS   = {"M": 4.0, "W": 3.5, "D": 3.0, "4H": 2.5, "1H": 2.0, "15m": 1.5}
TOTAL_WEIGHT = sum(TF_WEIGHTS.values())


def get_tf_trend(df: pd.DataFrame, tf_type: str):
    if not is_valid_df(df, 50):
        return 0, 40.0, "NEUT"
    close = df['close']

    if tf_type in ("M", "W"):
        min_bars = 52 if tf_type == "M" else 200
        if len(df) < min_bars:
            return 0, 40.0, "NEUT"
        sma200 = close.rolling(min(200, len(df))).mean().iloc[-1]
        ema50  = close.ewm(span=50, adjust=False).mean().iloc[-1]
        if pd.isna(sma200) or pd.isna(ema50):
            return 0, 40.0, "NEUT"
        trend    = 1 if ema50 > sma200 else (-1 if ema50 < sma200 else 0)
        strength = 75.0 if ema50 != sma200 else 40.0
        label    = "BULL" if trend == 1 else ("BEAR" if trend == -1 else "NEUT")
        return trend, strength, label

    elif tf_type == "4H":
        score = 0
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        score += 1 if close.iloc[-1] > ema50 else -1
        try:
            adx_s, pdi_s, mdi_s = QuantEngine.adx(df, 14)
            score += 1 if pdi_s.iloc[-1] > mdi_s.iloc[-1] else -1
        except Exception as _e:
            logger.warning("get_tf_trend 4H ADX error: %s", _e)

        # BUG-007 : filtrage journalier timezone-aware correct
        try:
            df_tz = df.copy()
            if df_tz.index.tz is None:
                df_tz.index = df_tz.index.tz_localize("UTC")
            today  = df_tz.index[-1].date()
            mask   = pd.Series(
                [ts.date() for ts in df_tz.index],
                index=df_tz.index
            ) == today
            day_rows = df_tz[mask]
            if not day_rows.empty:
                daily_open = day_rows['open'].iloc[0]
                score += 1 if close.iloc[-1] > daily_open else -1
        except Exception as _e:
            logger.warning("get_tf_trend 4H daily_open error: %s", _e)

        trend    = 1 if score > 0 else (-1 if score < 0 else 0)
        strength = 90.0 if abs(score) == 3 else (70.0 if abs(score) >= 1 else 40.0)
        label    = "BULL" if trend == 1 else ("BEAR" if trend == -1 else "NEUT")
        return trend, strength, label

    else:
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema9  = close.ewm(span=9,  adjust=False).mean()
        zl    = QuantEngine.zlema(close, 50, 17)
        rsi_v = QuantEngine.rsi(close, 14)
        macd  = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        sig   = macd.ewm(span=9, adjust=False).mean()
        c     = close.iloc[-1]
        bullish = (c > zl.iloc[-1] and ema9.iloc[-1] > ema21.iloc[-1] and
                   ema21.iloc[-1] > ema50.iloc[-1] and
                   not pd.isna(rsi_v.iloc[-1]) and rsi_v.iloc[-1] > 50 and
                   macd.iloc[-1] > sig.iloc[-1])
        bearish = (c < zl.iloc[-1] and ema9.iloc[-1] < ema21.iloc[-1] and
                   ema21.iloc[-1] < ema50.iloc[-1] and
                   not pd.isna(rsi_v.iloc[-1]) and rsi_v.iloc[-1] < 50 and
                   macd.iloc[-1] < sig.iloc[-1])
        base_str = min(80.0, abs(c - zl.iloc[-1]) / max(c, 1e-9) * 1000)
        strength = base_str if (bullish or bearish) else 30.0
        trend    = 1 if bullish else (-1 if bearish else 0)
        label    = "BULL" if trend == 1 else ("BEAR" if trend == -1 else "NEUT")
        return trend, strength, label


def compute_mtf_analysis(dfs: dict):
    results = {}
    for tf, df in dfs.items():
        t, s, lbl   = get_tf_trend(df, tf)
        results[tf] = {"trend": t, "strength": s, "label": lbl}

    macro_trend = 0
    for tf in ("M", "W", "D", "4H"):
        if tf in results and results[tf]["trend"] != 0:
            macro_trend = results[tf]["trend"]
            break

    t1h  = results.get("1H",  {}).get("trend", 0)
    t15m = results.get("15m", {}).get("trend", 0)
    f1h  = 0 if (macro_trend != 0 and macro_trend != t1h)  else t1h
    f15m = 0 if (macro_trend != 0 and macro_trend != t15m) else t15m

    # BUG-002 : alignment_pct calculé sur les tendances BRUTES avant mutation
    raw_bull      = sum(TF_WEIGHTS[tf] for tf in TF_WEIGHTS
                        if results.get(tf, {}).get("trend", 0) == 1)
    raw_bear      = sum(TF_WEIGHTS[tf] for tf in TF_WEIGHTS
                        if results.get(tf, {}).get("trend", 0) == -1)
    alignment_pct = round(max(raw_bull, raw_bear) / TOTAL_WEIGHT * 100)
    dominant      = ("Bullish" if raw_bull > raw_bear
                     else "Bearish" if raw_bear > raw_bull else "Neutral")

    # Mutation post-calcul
    if "1H"  in results: results["1H"]["trend"]  = f1h
    if "15m" in results: results["15m"]["trend"] = f15m

    return alignment_pct, dominant, results


# ----------------------------------------------------------------
#  DAILY BIAS  (5 facteurs)
# ----------------------------------------------------------------
def get_daily_bias_v2(df_d: pd.DataFrame, current_price: float = None):
    if not is_valid_df(df_d, 60):
        return "NEUTRAL", {}

    close = df_d['close']
    high  = df_d['high']
    low   = df_d['low']

    cur = current_price if current_price is not None else float(close.iloc[-1])

    votes_bull = 0
    votes_bear = 0
    detail     = {}

    # BUG-009 : comparaison float avec tolérance ε
    def _swing_pts(series, wing=5):
        highs, lows = [], []
        arr = series.to_numpy(dtype=np.float64)
        n   = len(arr)
        for i in range(wing, n - wing):
            window = arr[i - wing: i + wing + 1]
            if abs(arr[i] - window.max()) < 1e-9: highs.append(i)
            if abs(arr[i] - window.min()) < 1e-9: lows.append(i)
        return highs, lows

    sh_idx, _  = _swing_pts(high)
    _, sl_idx  = _swing_pts(low)

    struct_vote = "NEUTRAL"
    if len(sh_idx) >= 2 and len(sl_idx) >= 2:
        hh = high.iloc[sh_idx[-1]] > high.iloc[sh_idx[-2]]
        hl = low.iloc[sl_idx[-1]]  > low.iloc[sl_idx[-2]]
        lh = high.iloc[sh_idx[-1]] < high.iloc[sh_idx[-2]]
        ll = low.iloc[sl_idx[-1]]  < low.iloc[sl_idx[-2]]
        if hh and hl:
            struct_vote = "BULLISH"; votes_bull += 2
        elif lh and ll:
            struct_vote = "BEARISH"; votes_bear += 2
    detail["Structure"] = struct_vote

    ema50_series = close.ewm(span=50, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    ema50 = ema50_series.iloc[-1]
    if   cur > ema21 > ema50: detail["EMA 21/50"] = "BULLISH"; votes_bull += 1
    elif cur < ema21 < ema50: detail["EMA 21/50"] = "BEARISH"; votes_bear += 1
    else:                     detail["EMA 21/50"] = "NEUTRAL"

    # BUG-015 : Weekly Open = premier open du lundi de la semaine courante
    wo_vote = "NEUTRAL"
    try:
        df_copy = df_d.copy()
        if df_copy.index.tz is None:
            df_copy.index = df_copy.index.tz_localize("UTC")
        # Lundi uniquement (dayofweek == 0), semaine en cours
        monday_rows = df_copy[df_copy.index.dayofweek == 0]
        current_week_mondays = monday_rows[
            monday_rows.index >= (datetime.now(pytz.UTC) - pd.Timedelta(days=7))
        ]
        if not current_week_mondays.empty:
            weekly_open = float(current_week_mondays['open'].iloc[0])
            wo_vote = "BULLISH" if cur > weekly_open else "BEARISH"
            if wo_vote == "BULLISH": votes_bull += 1
            else:                    votes_bear += 1
        detail["Weekly Open"] = wo_vote
    except Exception as _e:
        logger.warning("get_daily_bias_v2 Weekly Open error: %s", _e)
        detail["Weekly Open"] = "NEUTRAL"

    if is_valid_df(df_d, 2):
        midpoint = (float(high.iloc[-2]) + float(low.iloc[-2])) / 2
        if float(close.iloc[-2]) > midpoint:
            detail["Close J-1"] = "BULLISH"; votes_bull += 1
        else:
            detail["Close J-1"] = "BEARISH"; votes_bear += 1
    else:
        detail["Close J-1"] = "NEUTRAL"

    # BUG-003 : slope_norm encapsulé correctement
    slope_vote = "NEUTRAL"
    slope_norm = 0.0
    try:
        if len(ema50_series) >= 6:
            atr_d_val = float(
                pd.concat([
                    high - low,
                    (high - close.shift()).abs(),
                    (low  - close.shift()).abs(),
                ], axis=1).max(axis=1).ewm(alpha=1/14, adjust=False).mean().iloc[-1]
            )
            slope_5d = float(ema50_series.iloc[-1] - ema50_series.iloc[-6])
            if atr_d_val > 0:
                slope_norm = slope_5d / atr_d_val
                if   slope_norm >  0.05: slope_vote = "BULLISH"; votes_bull += 1
                elif slope_norm < -0.05: slope_vote = "BEARISH"; votes_bear += 1
        detail["EMA50 Slope"] = f"{slope_vote} ({slope_norm:+.3f})"
    except Exception as _e:
        logger.warning("get_daily_bias_v2 EMA50 Slope error: %s", _e)
        detail["EMA50 Slope"] = "NEUTRAL"

    detail["Votes"] = f"{votes_bull}B / {votes_bear}S"

    if   votes_bull >= 5: bias = "STRONG BULLISH"
    elif votes_bull >= 3: bias = "BULLISH"
    elif votes_bear >= 5: bias = "STRONG BEARISH"
    elif votes_bear >= 3: bias = "BEARISH"
    else:                 bias = "NEUTRAL"

    return bias, detail


# ----------------------------------------------------------------
#  FVG M15
# ----------------------------------------------------------------
def detect_fvg(df, price, lookback=80):
    sub = df.iloc[-(lookback + 3):].reset_index(drop=True)
    n   = len(sub)
    if n < 3:
        return False, False, None, None

    denom     = sub["low"].replace(0, 1e-9)
    range_pct = (sub["high"] - sub["low"]) / denom
    auto_thr  = float(range_pct.expanding().mean().iloc[-1]) * 2.0

    active_bulls = []
    active_bears = []

    for i in range(2, n):
        h0 = float(sub["high"].iloc[i]);   l0 = float(sub["low"].iloc[i])
        c0 = float(sub["close"].iloc[i]);  c1 = float(sub["close"].iloc[i - 1])
        h2 = float(sub["high"].iloc[i - 2]); l2 = float(sub["low"].iloc[i - 2])

        # Invalidation : bull FVG invalidé si close sous le bas du gap
        active_bulls = [(b, t) for b, t in active_bulls if c0 >= b]
        # Invalidation : bear FVG invalidé si close au-dessus du haut du gap
        active_bears = [(b, t) for b, t in active_bears if c0 <= t]

        if l0 > h2 and c1 > h2:
            size_pct = (l0 - h2) / max(h2, 1e-9)
            if size_pct >= auto_thr:
                active_bulls.insert(0, (h2, l0))
        elif h0 < l2 and c1 < l2:
            size_pct = (l2 - h0) / max(h0, 1e-9)
            if size_pct >= auto_thr:
                active_bears.insert(0, (h0, l2))

    in_bull = any(b <= price <= t for b, t in active_bulls)
    in_bear = any(b <= price <= t for b, t in active_bears)

    def _nearest(lst):
        if not lst:
            return None
        return min(lst, key=lambda r: abs(price - (r[0] + r[1]) / 2))

    return (in_bull, in_bear, _nearest(active_bulls), _nearest(active_bears))


# ----------------------------------------------------------------
#  CURRENCY STRENGTH
# ----------------------------------------------------------------
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]

STRENGTH_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
    "AUD_USD", "USD_CAD", "NZD_USD",
    "EUR_GBP", "EUR_JPY", "EUR_CHF",
    "GBP_JPY", "AUD_JPY", "CAD_JPY", "NZD_JPY",
]

def compute_currency_strength(dfs_h1: dict) -> dict:
    raw    = {c: 0.0 for c in CURRENCIES}
    counts = {c: 0   for c in CURRENCIES}

    for pair, df in dfs_h1.items():
        if not is_valid_df(df, 60):
            continue
        parts = pair.split("_")
        if len(parts) != 2:
            continue
        base, quote = parts[0], parts[1]
        if base not in CURRENCIES or quote not in CURRENCIES:
            continue

        close  = df["close"]
        ema9   = close.ewm(span=9,  adjust=False).mean().iloc[-1]
        ema21  = close.ewm(span=21, adjust=False).mean().iloc[-1]
        ema50  = close.ewm(span=50, adjust=False).mean().iloc[-1]
        rsi_v  = QuantEngine.rsi(close, 14).iloc[-1]

        rsi_ok   = not pd.isna(rsi_v)
        bullish  = ema9 > ema21 > ema50 and rsi_ok and rsi_v > 50
        bearish  = ema9 < ema21 < ema50 and rsi_ok and rsi_v < 50
        contrib  = 1.0 if bullish else (-1.0 if bearish else 0.0)

        raw[base]  += contrib;  raw[quote]  -= contrib
        counts[base] += 1;      counts[quote] += 1

    scores = {}
    for c in CURRENCIES:
        scores[c] = raw[c] / counts[c] if counts[c] > 0 else 0.0

    vals = list(scores.values())
    s_min, s_max = min(vals), max(vals)
    spread = s_max - s_min
    if spread < 1e-8:
        return {c: 5.0 for c in CURRENCIES}
    return {c: round((v - s_min) / spread * 10, 2) for c, v in scores.items()}


def get_strength_delta(ticker: str, strength_scores: dict) -> float | None:
    parts = ticker.split("_")
    if len(parts) != 2:
        return None
    base, quote = parts[0], parts[1]
    sb = strength_scores.get(base)
    sq = strength_scores.get(quote)
    if sb is None or sq is None:
        return None
    return round(sb - sq, 2)


# ----------------------------------------------------------------
#  MOMENTUM SCORE
# ----------------------------------------------------------------
def compute_momentum_score(df_h4, df_h1, df_m15, signal_is_bull: bool) -> int:
    score = 0
    for df_src in (df_h4, df_h1, df_m15):
        if not is_valid_df(df_src, 20):
            continue
        try:
            adx_s, pdi_s, mdi_s = QuantEngine.adx(df_src, 14)
            adx_v = float(adx_s.iloc[-1])
            di_ok = (pdi_s.iloc[-1] > mdi_s.iloc[-1]) if signal_is_bull \
                    else (mdi_s.iloc[-1] > pdi_s.iloc[-1])
            if adx_v >= 25 and di_ok:
                score += 1
        except Exception as _e:
            logger.warning("compute_momentum_score error: %s", _e)
    return score


# ----------------------------------------------------------------
#  SYSTÈME DE NOTATION  V16
#  near_pdl / near_pdh supprimés (dead code — BUG-022)
# ----------------------------------------------------------------
def compute_score(flip_type, candles_ago,
                  mtf_pct, mtf_dominant,
                  zone_discount, zone_premium,
                  below_mid, above_mid,
                  in_bull_fvg, in_bear_fvg, fvg_near_bull, fvg_near_bear,
                  adx_val, pdi_val, mdi_val, atr_val, atr_mean,
                  midnight_bonus: bool = False,
                  adr_consumed: float | None = None):

    score        = 0
    score_detail = {}

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

    mtf_aligned = ((signal_is_bull and mtf_dominant == "Bullish") or
                   (signal_is_bear and mtf_dominant == "Bearish"))
    if mtf_aligned:
        if   mtf_pct >= 80: pts = 25
        elif mtf_pct >= 65: pts = 18
        elif mtf_pct >= 50: pts = 10
        else:               pts = 5
    else:
        pts = 0
    score += pts
    score_detail["MTF"] = pts

    if signal_is_bull:
        pts = 15 if zone_discount else (8 if below_mid else 0)
    elif signal_is_bear:
        pts = 15 if zone_premium  else (8 if above_mid else 0)
    else:
        pts = 0
    score += pts
    score_detail["Zone"] = pts

    if midnight_bonus:
        score += 3
        score_detail["Midnight"] = 3
    else:
        score_detail["Midnight"] = 0

    if signal_is_bull:
        pts = 15 if in_bull_fvg else (7 if fvg_near_bull else 0)
    elif signal_is_bear:
        pts = 15 if in_bear_fvg else (7 if fvg_near_bear else 0)
    else:
        pts = 0
    score += pts
    score_detail["FVG"] = pts

    adx_dir_ok = ((signal_is_bull and pdi_val > mdi_val) or
                  (signal_is_bear and mdi_val > pdi_val))
    if   adx_val > 25 and adx_dir_ok: pts = 10
    elif adx_val > 20 and adx_dir_ok: pts = 6
    elif adx_val > 20:                pts = 3
    else:                             pts = 0
    score += pts
    score_detail["ADX"] = pts

    if   atr_val >= atr_mean:       pts = 5
    elif atr_val >= atr_mean * 0.5: pts = 3
    else:                           pts = 0
    score += pts
    score_detail["ATR"] = pts

    # Malus range épuisé (V16)
    if adr_consumed is not None and adr_consumed > 70:
        score -= 5
        score_detail["ADR Malus"] = -5
    else:
        score_detail["ADR Malus"] = 0

    if   score >= 85: grade = "A+"
    elif score >= 70: grade = "A"
    elif score >= 55: grade = "B+"
    elif score >= 40: grade = "B"
    else:             grade = "C"

    return score, grade, score_detail


# ----------------------------------------------------------------
#  FLIP HMA
# ----------------------------------------------------------------
def find_last_hma_flip(hma_series, max_lookback=20):
    """BUG-014 : candles_ago garanti ≥ 0."""
    colors = []
    n      = len(hma_series)
    for i in range(n - 1, max(n - max_lookback - 2, 1), -1):
        v_curr = hma_series.iloc[i]
        v_prev = hma_series.iloc[i - 1]
        if pd.isna(v_curr) or pd.isna(v_prev):
            continue
        colors.append((i, "GREEN" if v_curr > v_prev else "RED"))

    for j in range(len(colors) - 1):
        idx_curr, col_curr = colors[j]
        _,        col_prev = colors[j + 1]
        if col_curr != col_prev:
            candles_ago = max(0, (n - 1) - idx_curr)
            return ("BULL" if col_curr == "GREEN" else "BEAR"), candles_ago
    return None, None


# ----------------------------------------------------------------
#  FETCH OANDA — client créé par appel (thread-safe, BUG-005)
# ----------------------------------------------------------------
MAX_RETRIES = 3
RETRY_DELAY = 1.5

def fetch_oanda_data(access_token: str, environment: str,
                     instrument: str, granularity: str, count: int):
    """Crée un client OANDA dédié pour chaque appel — thread-safe (BUG-005)."""
    client = oandapyV20.API(access_token=access_token, environment=environment)
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
            elif "429" in err_str:
                time.sleep(RETRY_DELAY * attempt * 3)
                continue          # BUG-019 : continue explicite
            elif "500" in err_str or "503" in err_str:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
            if attempt == MAX_RETRIES:
                return pd.DataFrame(), str(e)
    return pd.DataFrame(), "MAX_RETRIES"


def test_oanda_connection(access_token: str, environment: str, account_id: str = None):
    client = oandapyV20.API(access_token=access_token, environment=environment)
    try:
        if account_id:
            r = accounts.AccountDetails(account_id)
        else:
            r = accounts.AccountList()
        client.request(r)
        if account_id:
            acc      = r.response.get("account", {})
            currency = acc.get("currency", "?")
            balance  = acc.get("balance",  "?")
            return True, f"✅ Connecté — compte `{account_id}` · {currency} {balance}"
        else:
            accs = r.response.get("accounts", [])
            ids  = [a.get("id", "?") for a in accs]
            return True, f"✅ Connecté — {len(ids)} compte(s)"
    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err:
            return False, "❌ Token invalide ou expiré"
        return False, f"❌ Erreur : {err}"


# ----------------------------------------------------------------
#  FETCH STRENGTH — thread-safe via access_token (BUG-005)
# ----------------------------------------------------------------
def fetch_strength_data(access_token: str, environment: str) -> dict:
    dfs = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(fetch_oanda_data, access_token, environment, pair, "H1", 100): pair
            for pair in STRENGTH_PAIRS
        }
        for fut in as_completed(futures):
            pair = futures[fut]
            try:
                df, err = fut.result()
                if not df.empty:
                    dfs[pair] = df
            except Exception as _e:
                logger.warning("fetch_strength_data %s: %s", pair, _e)
    return dfs


# ----------------------------------------------------------------
#  ANALYSE PRINCIPALE — thread-safe (BUG-005)
# ----------------------------------------------------------------
def analyze_asset(access_token: str, environment: str,
                  ticker: str, freshness_limit_min: int = 30,
                  strength_scores: dict = None,
                  debug_log: list = None, _lock: threading.Lock = None):

    def _reject(reason):
        if debug_log is not None:
            if _lock:
                with _lock:
                    debug_log.append((ticker, reason))
            else:
                debug_log.append((ticker, reason))
        return None

    try:
        fetch_specs = [
            ("M15", 400), ("H1", 200), ("D", 300),
            ("H4", 300),  ("W", 250),  ("M", 80),
        ]
        fetch_results = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {
                ex.submit(fetch_oanda_data,
                          access_token, environment, ticker, gran, cnt): gran
                for gran, cnt in fetch_specs
            }
            for fut in as_completed(futures):
                gran = futures[fut]
                try:
                    fetch_results[gran] = fut.result()
                except Exception as _e:
                    logger.error("[%s] fetch %s exception: %s", ticker, gran, _e)
                    fetch_results[gran] = (pd.DataFrame(), str(_e))

        df_m15, err_m15 = fetch_results.get("M15", (pd.DataFrame(), "missing"))
        df_h1,  _       = fetch_results.get("H1",  (pd.DataFrame(), None))
        df_d,   err_d   = fetch_results.get("D",   (pd.DataFrame(), "missing"))
        df_4h,  _       = fetch_results.get("H4",  (pd.DataFrame(), None))
        df_w,   _       = fetch_results.get("W",   (pd.DataFrame(), None))
        df_mo,  _       = fetch_results.get("M",   (pd.DataFrame(), None))

        if not is_valid_df(df_m15): return _reject(f"FETCH_M15: {err_m15}")
        if not is_valid_df(df_d):   return _reject(f"FETCH_D: {err_d}")

        # BUG-013 : staleness check — données fraîches < 30 min
        try:
            last_candle_time = df_m15.index[-1]
            if last_candle_time.tz is None:
                last_candle_time = last_candle_time.tz_localize("UTC")
            now_utc           = datetime.now(pytz.UTC)
            staleness_minutes = (now_utc - last_candle_time).total_seconds() / 60
            if staleness_minutes > 30:
                return _reject(f"STALE_DATA: {staleness_minutes:.0f}min")
        except Exception as _e:
            logger.warning("[%s] staleness check error: %s", ticker, _e)

        price = float(df_m15['close'].iloc[-1])

        bias, bias_detail = get_daily_bias_v2(df_d, current_price=price)
        bias_bull = bias in ("BULLISH", "STRONG BULLISH")
        bias_bear = bias in ("BEARISH", "STRONG BEARISH")

        dfs_mtf = {
            "M":   df_mo  if is_valid_df(df_mo)  else None,
            "W":   df_w   if is_valid_df(df_w)   else None,
            "D":   df_d   if is_valid_df(df_d)   else None,
            "4H":  df_4h  if is_valid_df(df_4h)  else None,
            "1H":  df_h1  if is_valid_df(df_h1)  else None,
            "15m": df_m15 if is_valid_df(df_m15) else None,
        }
        mtf_pct, mtf_dominant, mtf_details = compute_mtf_analysis(dfs_mtf)

        hma = QuantEngine.hma(df_m15['close'], 20)
        if hma.isna().iloc[-5:].any():
            return _reject("HMA_NAN")

        flip_type, candles_ago = find_last_hma_flip(hma, max_lookback=20)
        if flip_type is None:
            return _reject("NO_HMA_FLIP")

        if bias == "NEUTRAL":
            return _reject("BIAS_NEUTRAL")

        if (flip_type == "BULL" and bias_bull) or (flip_type == "BEAR" and bias_bear):
            bias_alignment = "ALIGNED"
        else:
            bias_alignment = "COUNTER"

        mins_ago      = candles_ago * 15
        freshness_str = f"⚡ {mins_ago} min" if mins_ago <= freshness_limit_min else f"⏳ {mins_ago} min"
        signal_fresh  = mins_ago <= freshness_limit_min

        hma_now = hma.iloc[-1]
        if flip_type == "BULL" and price <= hma_now:
            return _reject("PRICE_BELOW_HMA_ON_BULL")
        if flip_type == "BEAR" and price >= hma_now:
            return _reject("PRICE_ABOVE_HMA_ON_BEAR")

        atr_m15  = QuantEngine.atr(df_m15, 14)
        atr_val  = float(atr_m15.iloc[-1])
        atr_mean = float(atr_m15.iloc[-50:].mean())

        in_bull_fvg, in_bear_fvg, nb_fvg, nr_fvg = detect_fvg(df_m15, price, lookback=80)
        fvg_near_bull = (nb_fvg is not None and
                         abs(price - (nb_fvg[0] + nb_fvg[1]) / 2) < atr_val * 1.0)
        fvg_near_bear = (nr_fvg is not None and
                         abs(price - (nr_fvg[0] + nr_fvg[1]) / 2) < atr_val * 1.0)

        # ── MIDNIGHT OPEN — avec validation heure exacte (BUG-010) ──
        midnight_open             = None
        midnight_open_approximate = False
        try:
            ny_tz     = pytz.timezone("America/New_York")
            _m15_raw  = pd.to_datetime(df_m15.index)
            if _m15_raw.tz is None:
                m15_times = _m15_raw.tz_localize("UTC").tz_convert(ny_tz)
            else:
                m15_times = _m15_raw.tz_convert(ny_tz)
            today_ny = datetime.now(ny_tz).date()
            mn_mask  = (
                (np.array([t.date() for t in m15_times]) == today_ny) &
                (np.array([t.hour   for t in m15_times]) == 0) &
                (np.array([t.minute for t in m15_times]) == 0)
            )
            mn_c = df_m15[mn_mask]
            if mn_c.empty:
                # Fallback : première bougie du jour — approximatif
                today_mask = np.array([t.date() for t in m15_times]) == today_ny
                today_c    = df_m15[today_mask]
                if not today_c.empty:
                    first_time = m15_times[today_mask][0]
                    # BUG-010 : signaler l'approximation si heure != 00:00
                    if first_time.hour != 0 or first_time.minute != 0:
                        midnight_open_approximate = True
                    midnight_open = float(today_c["open"].iloc[0])
            else:
                midnight_open = float(mn_c["open"].iloc[0])
        except Exception as _e:
            logger.warning("[%s] midnight_open error: %s", ticker, _e)

        # ── ZONE DISCOUNT / PREMIUM — BUG-001 : _d1_ref = -1 ──────
        # df_d ne contient que des bougies complètes → iloc[-1] = J-1 (correct)
        assert is_valid_df(df_d, 1), "df_d vide — PDH/PDL invalide"
        pdh = float(df_d["high"].iloc[-1])
        pdl = float(df_d["low"].iloc[-1])

        if midnight_open is not None and not midnight_open_approximate:
            in_premium       = (price > midnight_open) and (price <= pdh)
            in_discount      = (price < midnight_open) and (price >= pdl)
            in_extended_high = price > pdh
            in_extended_low  = price < pdl

            if in_extended_high:
                zone_label = "EXT HIGH"
            elif in_extended_low:
                zone_label = "EXT LOW"
            elif in_premium:
                zone_label = "PREMIUM"
            elif in_discount:
                zone_label = "DISCOUNT"
            else:
                zone_label = "EQUILIBRE"

            zone_discount = in_discount
            zone_premium  = in_premium
            below_mid     = in_discount
            above_mid     = in_premium
        else:
            d1_mid        = (pdh + pdl) / 2.0
            in_discount   = price < d1_mid
            in_premium    = price > d1_mid
            zone_discount = in_discount
            zone_premium  = in_premium
            below_mid     = in_discount
            above_mid     = in_premium
            zone_label    = ("DISCOUNT" if in_discount else
                             "PREMIUM"  if in_premium  else "EQUILIBRE")

        midnight_bonus = False
        if midnight_open is not None and not midnight_open_approximate:
            if flip_type == "BULL" and price < midnight_open:
                midnight_bonus = True
            elif flip_type == "BEAR" and price > midnight_open:
                midnight_bonus = True

        df_adx_src            = df_h1 if is_valid_df(df_h1, 20) else df_m15
        adx_s, pdi_s, mdi_s  = QuantEngine.adx(df_adx_src, 14)
        adx_val_score = round(float(adx_s.iloc[-1]), 1)
        pdi_val       = round(float(pdi_s.iloc[-1]), 1)
        mdi_val       = round(float(mdi_s.iloc[-1]), 1)

        # ADR (forex/metals) ou ATR (indices)
        if ticker in INDICES:
            _atr_d_raw   = QuantEngine.atr(df_d, 14).iloc[-1]
            adr_display  = round(float(_atr_d_raw), 2) if not pd.isna(_atr_d_raw) else None
            adr_label    = "ATR"
            adr_consumed = None
        else:
            _adr_raw    = compute_adr(df_d, period=14)
            adr_display = round(float(_adr_raw), 5) if not np.isnan(_adr_raw) else None
            adr_label   = "ADR"
            adr_consumed = compute_adr_consumed(df_m15, midnight_open, adr_display)
            if adr_consumed is not None and np.isnan(adr_consumed):
                adr_consumed = None

        score, grade, score_detail = compute_score(
            flip_type, candles_ago, mtf_pct, mtf_dominant,
            zone_discount, zone_premium,
            below_mid, above_mid,
            in_bull_fvg, in_bear_fvg, fvg_near_bull, fvg_near_bear,
            adx_val_score, pdi_val, mdi_val, atr_val, atr_mean,
            midnight_bonus=midnight_bonus,
            adr_consumed=adr_consumed,
        )

        strength_delta = None
        if strength_scores:
            strength_delta = get_strength_delta(ticker, strength_scores)

        signal_is_bull = (flip_type == "BULL")   # calculé une seule fois (BUG-022)
        _df_h4  = df_4h if is_valid_df(df_4h) else None
        _df_h1  = df_h1 if is_valid_df(df_h1) else None
        momentum = compute_momentum_score(_df_h4, _df_h1, df_m15, signal_is_bull)

        sig = (("▲ LONG"  if flip_type == "BULL" else "▼ SHORT")
               if signal_fresh else
               ("LONG (expiré)" if flip_type == "BULL" else "SHORT (expiré)"))

        return {
            "Fraîcheur":      freshness_str,
            "Actif + Note":   ticker,
            "Signal":         sig,
            "Biais Daily":    bias,
            "Alignement":     bias_alignment,
            "Zone":           zone_label,
            "Score /100":     score,
            "Grade":          grade,
            "ADX H1":         adx_val_score,
            "ADR":            adr_display,
            "ADR Label":      adr_label,
            "ADR Consumed":   adr_consumed,
            "MTF Pct":        mtf_pct,
            "MTF Dom":        mtf_dominant,
            "MTF Details":    mtf_details,
            "Strength Δ":     strength_delta,
            "Momentum":       momentum,
            "signal_is_bull": signal_is_bull,
            "Midnight Bonus": midnight_bonus,
        }

    except Exception as e:
        logger.error("[%s] analyze_asset EXCEPTION: %s", ticker, e, exc_info=True)
        if debug_log is not None:
            if _lock:
                with _lock:
                    debug_log.append((ticker, f"EXCEPTION: {e}"))
            else:
                debug_log.append((ticker, f"EXCEPTION: {e}"))
        return None


# ----------------------------------------------------------------
#  INTERFACE STREAMLIT
# ----------------------------------------------------------------
def main():
    st.set_page_config(
        page_title="BLUESTAR SNIPER V16",
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

    # Killzone calculée une seule fois (BUG-022 — duplication supprimée)
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
          BLUESTAR SNIPER V16 {kz_html}
        </h1>
        <p style="margin:0;color:#4a4a7a;font-family:'IBM Plex Mono',monospace;font-size:12px;letter-spacing:.1em">
          HMA 20 M15 · MTF INSTITUTIONAL · ADR DAILY · FORCE H4·H1·M15 · LUXALGO FVG
        </p>
      </div>
    </div>
    <hr>
    """, unsafe_allow_html=True)

    missing = [k for k in ("OANDA_ACCESS_TOKEN", "OANDA_ACCOUNT_ID") if k not in st.secrets]
    if missing:
        st.error(f"🔑 **Secret(s) manquant(s) :** `{'`, `'.join(missing)}`")
        st.stop()

    ACCESS_TOKEN = st.secrets["OANDA_ACCESS_TOKEN"]
    ACCOUNT_ID   = st.secrets["OANDA_ACCOUNT_ID"]

    # BUG-020 : environnement configurable via secrets
    env = st.secrets.get("OANDA_ENVIRONMENT", "practice")
    if env not in ("practice", "live"):
        st.error(f"⚠️ OANDA_ENVIRONMENT invalide : `{env}` — doit être `practice` ou `live`")
        st.stop()
    if env == "live":
        st.warning("⚠️ **CONNEXION COMPTE RÉEL (live)** — opérations sur capital réel")

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        freshness  = st.selectbox("Fraîcheur max du signal (min)", [15, 30, 45, 60], index=1)
    with col2:
        min_grade  = st.selectbox("Grade minimum", ["Tous", "B", "B+", "A", "A+"], index=0)
    with col3:
        show_debug = st.toggle("🐛 Debug", value=False)

    with st.expander("📘 Grille de notation V16"):
        st.markdown("""
| Critère | Max | Détail |
|---|---|---|
| **Trigger HMA flip** | 30 pts | ≤15 min = 30 · ≤30 min = 20 · ≤45 min = 10 · >45 min = 0 |
| **MTF Alignment** | 25 pts | ≥80% = 25 · ≥65% = 18 · ≥50% = 10 · contre-MTF = 0 |
| **Zone D1** | 15 pts | Discount/Premium = 15 (MO comme équilibre) · Zone seule = 8 |
| **Midnight Bonus** | 3 pts | Confluence directionnelle avec MO |
| **FVG M15** | 15 pts | LuxAlgo exact (mitigation + seuil auto) |
| **ADX momentum** | 10 pts | ADX>25+DI ok = 10 · ADX>20+DI ok = 6 |
| **ATR actif** | 5 pts | ≥ moyenne = 5 · ≥ 50% = 3 |
| **ADR Malus** | -5 pts | Range journalier > 70% consommé depuis minuit NY |

| Colonne | Description |
|---|---|
| **Zone** | DISCOUNT = prix sous MO (00h00 NY) · PREMIUM = prix dessus · EXT HIGH/LOW = hors PDH/PDL |
| **ADR / ATR** | Valeur + jauge de consommation du range (🟢 <40% · 🟡 40–70% · 🔴 >70%) |
| **Force** | ■■■ = ADX H4·H1·M15 ≥ 25 alignés · chiffre = Strength Δ base/quote |
        """)

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
    _lock     = threading.Lock()
    progress  = st.progress(0)
    status    = st.empty()
    t_start   = time.time()

    strength_scores = {}
    with st.spinner("Calcul Currency Strength H1…"):
        dfs_h1 = fetch_strength_data(ACCESS_TOKEN, env)
        strength_scores = compute_currency_strength(dfs_h1)

    # BUG-017 : boucle asset parallélisée — suppression du sleep(0.15)
    with st.spinner("Analyse MTF en cours…"):
        completed_count = 0
        max_workers     = min(8, len(assets))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_map = {
                ex.submit(
                    analyze_asset,
                    ACCESS_TOKEN, env, ticker,
                    freshness, strength_scores, debug_log, _lock
                ): ticker
                for ticker in assets
            }
            for fut in as_completed(future_map):
                ticker = future_map[fut]
                try:
                    res = fut.result()
                    if res:
                        with _lock:
                            results.append(res)
                except Exception as _e:
                    logger.error("Outer future %s: %s", ticker, _e)
                completed_count += 1
                progress.progress(completed_count / len(assets))
                status.caption(f"⏳ {ticker}… ({completed_count}/{len(assets)})")

    elapsed = round(time.time() - t_start, 1)
    status.empty()
    progress.empty()

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

    grade_order = {"A+": 5, "A": 4, "B+": 3, "B": 2, "C": 1}
    min_map     = {"Tous": 0, "B": 2, "B+": 3, "A": 4, "A+": 5}
    min_val     = min_map[min_grade]
    df = df[df["Grade"].map(grade_order) >= min_val]

    if df.empty:
        st.info("Aucun signal avec ces filtres.")
        return

    def sort_key(row):
        fresh = 1 if "⚡" in str(row["Fraîcheur"]) else 0
        g     = grade_order.get(row["Grade"], 0)
        s     = row["Score /100"]
        m     = row["MTF Pct"]
        mom   = row.get("Momentum", 0)
        return (-fresh, -g, -mom, -s, -m)

    df["_sk"] = df.apply(sort_key, axis=1)
    df = df.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)

    a_plus  = len(df[(df["Grade"] == "A+") & df["Fraîcheur"].str.contains("⚡", na=False)])
    a_grade = len(df[(df["Grade"] == "A")  & df["Fraîcheur"].str.contains("⚡", na=False)])
    b_watch = len(df[df["Grade"].isin(["B+","B"]) & df["Fraîcheur"].str.contains("⚡", na=False)])
    longs   = len(df[df["Signal"].str.contains("LONG",  na=False) & ~df["Signal"].str.contains("expiré")])
    shorts  = len(df[df["Signal"].str.contains("SHORT", na=False) & ~df["Signal"].str.contains("expiré")])
    max_mom = len(df[df.get("Momentum", 0) == 3]) if "Momentum" in df.columns else 0

    mc = st.columns(6)
    with mc[0]: st.metric("💎 Signaux A+", a_plus)
    with mc[1]: st.metric("🥇 Signaux A",  a_grade)
    with mc[2]: st.metric("👀 Watch B/B+", b_watch)
    with mc[3]: st.metric("▲ LONG actifs", longs)
    with mc[4]: st.metric("▼ SHORT actifs", shorts)
    with mc[5]: st.metric("■■■ Force max", max_mom)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── STYLES ───────────────────────────────────────────────────
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

    def bias_daily_label(b):
        if b == "STRONG BULLISH": return "▲▲ STRONG BULL"
        if b == "BULLISH":        return "▲ BULL"
        if b == "STRONG BEARISH": return "▼▼ STRONG BEAR"
        if b == "BEARISH":        return "▼ BEAR"
        return "— NEUTRAL"

    def bias_daily_style(b):
        if "STRONG BULLISH" in b: return "color:#4d9467;font-weight:700;font-size:14px"
        if "BULLISH"        in b: return "color:#3d7055;font-weight:600;font-size:14px"
        if "STRONG BEARISH" in b: return "color:#a04848;font-weight:700;font-size:14px"
        if "BEARISH"        in b: return "color:#7a3535;font-weight:600;font-size:14px"
        return "color:#404055;font-size:14px"

    def zone_style(z):
        if "DISCOUNT" in z: return "color:#4a7898;font-weight:600"
        if "PREMIUM"  in z: return "color:#8a6028;font-weight:600"
        if "EXT HIGH" in z: return "color:#9e4a3a;font-weight:600;font-style:italic"
        if "EXT LOW"  in z: return "color:#3a7a9e;font-weight:600;font-style:italic"
        return "color:#404055"

    def fresh_style(f):
        return "color:#9a7820;font-weight:700" if "⚡" in f else "color:#303040"

    def score_bar(score):
        """BUG-021 : gestion correcte des scores négatifs."""
        color = "#5a9e7a" if score >= 70 else "#9a7820" if score >= 40 else "#9e4a3a"
        width = max(4, min(score, 100)) if score >= 0 else 4
        label = str(score)                                   # affiche -5, -3, etc.
        return f"""<div style="display:flex;align-items:center;gap:8px">
          <div style="width:60px;height:4px;background:#1a1a22;border-radius:2px;overflow:hidden">
            <div style="width:{width}%;height:100%;background:{color};border-radius:2px"></div>
          </div>
          <span style="color:{color};font-weight:700;font-size:15px">{label}</span>
        </div>"""

    def adr_cell(v, label="ADR", consumed: float | None = None):
        if v is None:
            return '<span style="color:#303040">—</span>'

        formatted = f"{v:.5f}" if v < 1.0 else f"{v:.2f}"
        tag_color = "#4a7898" if label == "ADR" else "#6a5a78"

        tag_html = (
            f'<span style="color:{tag_color};font-size:10px;font-family:\'IBM Plex Mono\','
            f'monospace;margin-right:4px">{label}</span>'
            f'<span style="color:#5a6880;font-family:\'IBM Plex Mono\','
            f'monospace;font-size:13px">{formatted}</span>'
        )

        if consumed is None or (isinstance(consumed, float) and np.isnan(consumed)):
            return tag_html

        consumed = float(consumed)

        if consumed < 40:
            bar_color = "#5a9e7a"
            pct_color = "#5a9e7a"
        elif consumed < 70:
            bar_color = "#9a7820"
            pct_color = "#9a7820"
        else:
            bar_color = "#9e4a3a"
            pct_color = "#9e4a3a"

        fill_width = int(min(max(consumed, 0), 100))

        gauge_html = (
            f'<div style="display:flex;align-items:center;gap:6px;margin-top:4px">'
            f'<div style="width:52px;height:3px;background:#1a1a22;border-radius:2px;overflow:hidden">'
            f'<div style="width:{fill_width}%;height:100%;background:{bar_color};border-radius:2px"></div>'
            f'</div>'
            f'<span style="color:{pct_color};font-size:10px;font-family:\'IBM Plex Mono\','
            f'monospace;font-weight:700">{consumed:.0f}%</span>'
            f'</div>'
        )

        return f'<div>{tag_html}{gauge_html}</div>'

    def force_cell(momentum_score: int, delta, is_bull: bool) -> str:
        color    = "#5a9e7a" if is_bull else "#9e4a3a"
        filled   = f'background:{color};border-radius:2px'
        empty    = 'background:#1e1e2e;border-radius:2px'
        segments = "".join(
            f'<div style="width:14px;height:9px;{filled if i < momentum_score else empty}"></div>'
            for i in range(3)
        )
        bars = (f'<div style="display:flex;gap:3px;align-items:center">'
                f'{segments}</div>')

        if delta is not None:
            delta_color = ("#5a9e7a" if delta >= 1.5
                           else "#9e4a3a" if delta <= -1.5
                           else "#6a7888")
            sign  = "+" if delta > 0 else ""
            delta_html = (f'<span style="color:{delta_color};font-weight:700;'
                          f'font-size:12px;font-family:\'IBM Plex Mono\','
                          f'monospace;margin-left:7px">{sign}{delta:.1f}</span>')
        else:
            delta_html = '<span style="color:#303040;margin-left:7px;font-size:11px">n/a</span>'

        return (f'<div style="display:flex;align-items:center">'
                f'{bars}{delta_html}</div>')

    html = """
<style>
.sc-wrap{overflow-x:auto;margin-top:4px}
.sc-tbl{width:100%;border-collapse:collapse;font-family:'IBM Plex Mono','Courier New',monospace;font-size:15px}
.sc-tbl thead tr{border-bottom:2px solid #2a2a5a}
.sc-tbl th{
  padding:10px 16px;text-align:left;
  color:#8090c0;
  font-size:12px;text-transform:uppercase;letter-spacing:.12em;font-weight:700;
  white-space:nowrap;
  background:#0c0c12;
  border-bottom:2px solid #2a2a5a;
}
.sc-tbl td{padding:13px 16px;border-bottom:1px solid #0f0f1e;vertical-align:middle}
.sc-tbl tr:hover td{background:#111118 !important}
.ticker{font-size:22px;font-weight:800;color:#c8c8d8;letter-spacing:.04em;white-space:nowrap;font-family:'IBM Plex Mono',monospace}
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
</style>
<div class="sc-wrap"><table class="sc-tbl">
<thead><tr>
  <th style="width:100px">Fraîcheur</th>
  <th>Actif</th>
  <th>Signal</th>
  <th>Biais Daily</th>
  <th>Zone</th>
  <th>Score /100</th>
  <th>ADR / ATR</th>
  <th>Force</th>
</tr></thead><tbody>
"""

    for _, row in df.iterrows():
        grade       = str(row.get("Grade", "C"))
        fresh       = "⚡" in str(row.get("Fraîcheur", ""))
        gs          = GRADE_STYLE.get(grade, GRADE_STYLE["C"])
        score       = int(row.get("Score /100", 0))
        ticker      = str(row.get("Actif + Note", "")).split("  ")[0].strip()
        sig         = str(row.get("Signal", "—"))
        adr_v       = row.get("ADR")
        adr_lbl     = str(row.get("ADR Label", "ADR"))
        adr_cons    = row.get("ADR Consumed")
        bias        = str(row.get("Biais Daily", "—"))
        alignment   = str(row.get("Alignement", "ALIGNED"))
        zone        = str(row.get("Zone", "—"))
        fresh_str   = str(row.get("Fraîcheur", "—"))
        sdelta      = row.get("Strength Δ")
        momentum    = int(row.get("Momentum", 0))
        is_bull     = bool(row.get("signal_is_bull", True))
        mn_bonus    = bool(row.get("Midnight Bonus", False))

        align_tag = ""
        if alignment == "COUNTER":
            align_tag = '<span style="font-size:10px;color:#9e4a3a;font-weight:700;margin-left:6px">⚠ COUNTER</span>'

        kz_tag = (f'<span class="kz-badge">{killzone_badge(kz_now)}</span>'
                  if kz_now and fresh else "")

        mn_tag = ('<span style="font-size:10px;color:#5a7898;font-weight:700;'
                  'margin-left:6px;vertical-align:middle">🌙</span>'
                  if mn_bonus else "")

        row_bg = gs["bg"] if fresh and grade in ("A+", "A") else "transparent"

        html += f"""<tr style="background:{row_bg}">
  <td style="{fresh_style(fresh_str)}">{fresh_str}</td>
  <td style="white-space:nowrap">
    <span class="ticker">{ticker}</span>
    <span class="grade-pill" style="color:{gs['color']}">{gs['label']}</span>
    {align_tag}
  </td>
  <td style="{sig_style(sig)}">{sig}{kz_tag}</td>
  <td style="{bias_daily_style(bias)}">{bias_daily_label(bias)}</td>
  <td style="{zone_style(zone)}">{zone}{mn_tag}</td>
  <td>{score_bar(score)}</td>
  <td>{adr_cell(adr_v, adr_lbl, consumed=adr_cons)}</td>
  <td>{force_cell(momentum, sdelta, is_bull)}</td>
</tr>"""

    html += "</tbody></table></div>"
    st.markdown(html, unsafe_allow_html=True)

    st.markdown(f"""
    <div style="color:#2a2a4a;font-family:'IBM Plex Mono',monospace;font-size:10px;
                text-align:right;margin-top:6px;letter-spacing:.06em">
      {len(df)} signal(s) · scanné en {elapsed}s · {datetime.now().strftime('%H:%M:%S')}
      {'· <span style="color:#5a9e7a">KZ ' + kz_now + ' ACTIVE</span>' if kz_now else ''}
    </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
