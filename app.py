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
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================================================================
#  BLUESTAR SNIPER V15  —  ICT SIGNAL ENGINE
#  Modifications vs V14 :
#  ✅  ADR (Average Daily Range) pour forex/metals
#      ATR Daily conservé pour indices (US30, NAS100, DE30)
#      Colonne renommée "ADR / ATR" avec label par ligne
#  ✅  Zone PREMIUM / DISCOUNT — ICT pur (patch V15 final)
#      PREMIUM  = price > Midnight Open  ET price ≤ PDH
#      DISCOUNT = price < Midnight Open  ET price ≥ PDL
#      EXT HIGH / EXT LOW = hors range PDH/PDL
#      Fallback midpoint D1 si Midnight Open absent
#      Midnight Open calculé EN PREMIER (avant la zone)
# ================================================================


# ----------------------------------------------------------------
#  KILLZONE DETECTOR  (UTC+1 — Paris / Tunis)
# ----------------------------------------------------------------
KILLZONES_UTC1 = {
    "London": ((8,  0), (9,  30)),
    "NY AM":  ((13, 0), (14, 30)),
    "Asia":   ((2,  0), (4,  0)),
}

INDICES = {"US30_USD", "NAS100_USD", "DE30_EUR"}

def get_current_killzone() -> str:
    tz_utc1 = pytz.timezone("Europe/Paris")
    now = datetime.now(tz_utc1)
    t = now.hour * 60 + now.minute
    for name, (start, end) in KILLZONES_UTC1.items():
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
#  ADR — Average Daily Range (forex/metals uniquement)
# ----------------------------------------------------------------
def compute_adr(df_d: pd.DataFrame, period: int = 14) -> float:
    if df_d is None or len(df_d) < 2:
        return float("nan")
    ranges = (df_d["high"] - df_d["low"]).iloc[-(period + 1):-1]
    return float(ranges.mean()) if not ranges.empty else float("nan")


# ----------------------------------------------------------------
#  MTF INSTITUTIONAL TREND
# ----------------------------------------------------------------
TF_WEIGHTS = {"M": 4.0, "W": 3.5, "D": 3.0, "4H": 2.5, "1H": 2.0, "15m": 1.5}
TOTAL_WEIGHT = sum(TF_WEIGHTS.values())


def get_tf_trend(df: pd.DataFrame, tf_type: str):
    if df is None or len(df) < 50:
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
        adx_s, pdi_s, mdi_s = QuantEngine.adx(df, 14)
        score += 1 if pdi_s.iloc[-1] > mdi_s.iloc[-1] else -1
        idx = df.index.tz_localize("UTC") if df.index.tz is None else df.index
        today = idx[-1].date()
        day_rows = df[idx.date == today]
        if not day_rows.empty:
            daily_open = day_rows['open'].iloc[0]
            score += 1 if close.iloc[-1] > daily_open else -1
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
        c = close.iloc[-1]
        bullish = (c > zl.iloc[-1] and ema9.iloc[-1] > ema21.iloc[-1] and
                   ema21.iloc[-1] > ema50.iloc[-1] and rsi_v.iloc[-1] > 50 and
                   macd.iloc[-1]  > sig.iloc[-1])
        bearish = (c < zl.iloc[-1] and ema9.iloc[-1] < ema21.iloc[-1] and
                   ema21.iloc[-1] < ema50.iloc[-1] and rsi_v.iloc[-1] < 50 and
                   macd.iloc[-1]  < sig.iloc[-1])
        base_str = min(80.0, abs(c - zl.iloc[-1]) / c * 1000)
        strength = base_str if (bullish or bearish) else 30.0
        trend    = 1 if bullish else (-1 if bearish else 0)
        label    = "BULL" if trend == 1 else ("BEAR" if trend == -1 else "NEUT")
        return trend, strength, label


def compute_mtf_analysis(dfs: dict):
    results = {}
    for tf, df in dfs.items():
        t, s, lbl = get_tf_trend(df, tf)
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
    results["1H"]["trend"]  = f1h
    results["15m"]["trend"] = f15m

    bull_score = sum(TF_WEIGHTS[tf] for tf in TF_WEIGHTS if results.get(tf, {}).get("trend", 0) == 1)
    bear_score = sum(TF_WEIGHTS[tf] for tf in TF_WEIGHTS if results.get(tf, {}).get("trend", 0) == -1)
    alignment_pct = round(max(bull_score, bear_score) / TOTAL_WEIGHT * 100)
    dominant      = ("Bullish" if bull_score > bear_score
                     else "Bearish" if bear_score > bull_score else "Neutral")
    return alignment_pct, dominant, results


# ----------------------------------------------------------------
#  DAILY BIAS  (5 facteurs)
# ----------------------------------------------------------------
def get_daily_bias_v2(df_d: pd.DataFrame, current_price: float = None):
    if df_d is None or len(df_d) < 60:
        return "NEUTRAL", {}

    close = df_d['close']
    high  = df_d['high']
    low   = df_d['low']

    cur = current_price if current_price is not None else float(close.iloc[-1])

    votes_bull = 0
    votes_bear = 0
    detail = {}

    def _swing_pts(series, wing=5):
        highs, lows = [], []
        for i in range(wing, len(series) - wing):
            w = series.iloc[i - wing: i + wing + 1]
            if series.iloc[i] == w.max(): highs.append(i)
            if series.iloc[i] == w.min(): lows.append(i)
        return highs, lows

    sh_idx, _   = _swing_pts(high)
    _,  sl_idx  = _swing_pts(low)

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

    wo_vote = "NEUTRAL"
    try:
        df_copy = df_d.copy()
        if df_copy.index.tz is None:
            df_copy.index = df_copy.index.tz_localize("UTC")
        weekly_open_rows = df_copy[df_copy.index.dayofweek.isin([0, 6])]
        if not weekly_open_rows.empty:
            weekly_open = float(weekly_open_rows['open'].iloc[-1])
            wo_vote = "BULLISH" if cur > weekly_open else "BEARISH"
            if wo_vote == "BULLISH": votes_bull += 1
            else:                   votes_bear += 1
    except Exception:
        pass
    detail["Weekly Open"] = wo_vote

    if len(df_d) >= 2:
        midpoint = (float(high.iloc[-2]) + float(low.iloc[-2])) / 2
        if float(close.iloc[-2]) > midpoint:
            detail["Close J-1"] = "BULLISH"; votes_bull += 1
        else:
            detail["Close J-1"] = "BEARISH"; votes_bear += 1
    else:
        detail["Close J-1"] = "NEUTRAL"

    slope_vote = "NEUTRAL"
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
                if   slope_norm >  0.05:
                    slope_vote = "BULLISH"; votes_bull += 1
                elif slope_norm < -0.05:
                    slope_vote = "BEARISH"; votes_bear += 1
            detail["EMA50 Slope"] = f"{slope_vote} ({slope_norm:+.3f})"
    except Exception:
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

        active_bulls = [(b, t) for b, t in active_bulls if c0 >= b]
        active_bears = [(b, t) for b, t in active_bears if c0 <= t]

        if l0 > h2 and c1 > h2:
            size_pct = (l0 - h2) / h2 if h2 > 0 else 0.0
            if size_pct >= auto_thr:
                active_bulls.insert(0, (h2, l0))
        elif h0 < l2 and c1 < l2:
            size_pct = (l2 - h0) / h0 if h0 > 0 else 0.0
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
    raw = {c: 0.0 for c in CURRENCIES}
    counts = {c: 0 for c in CURRENCIES}

    for pair, df in dfs_h1.items():
        if df is None or df.empty or len(df) < 60:
            continue
        parts = pair.split("_")
        if len(parts) != 2:
            continue
        base, quote = parts[0], parts[1]
        if base not in CURRENCIES or quote not in CURRENCIES:
            continue

        close = df["close"]
        ema9  = close.ewm(span=9,  adjust=False).mean().iloc[-1]
        ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        rsi_v = QuantEngine.rsi(close, 14).iloc[-1]

        bullish = ema9 > ema21 > ema50 and rsi_v > 50
        bearish = ema9 < ema21 < ema50 and rsi_v < 50
        contrib = 1.0 if bullish else (-1.0 if bearish else 0.0)

        raw[base]  += contrib
        raw[quote] -= contrib
        counts[base]  += 1
        counts[quote] += 1

    scores = {}
    for c in CURRENCIES:
        if counts[c] > 0:
            scores[c] = raw[c] / counts[c]
        else:
            scores[c] = 0.0

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

    try:
        if df_h4 is not None and len(df_h4) >= 20:
            adx_h4, pdi_h4, mdi_h4 = QuantEngine.adx(df_h4, 14)
            adx_v = float(adx_h4.iloc[-1])
            di_ok = (pdi_h4.iloc[-1] > mdi_h4.iloc[-1]) if signal_is_bull else (mdi_h4.iloc[-1] > pdi_h4.iloc[-1])
            if adx_v >= 25 and di_ok:
                score += 1
    except Exception:
        pass

    try:
        if df_h1 is not None and len(df_h1) >= 20:
            adx_h1, pdi_h1, mdi_h1 = QuantEngine.adx(df_h1, 14)
            adx_v = float(adx_h1.iloc[-1])
            di_ok = (pdi_h1.iloc[-1] > mdi_h1.iloc[-1]) if signal_is_bull else (mdi_h1.iloc[-1] > pdi_h1.iloc[-1])
            if adx_v >= 25 and di_ok:
                score += 1
    except Exception:
        pass

    try:
        if df_m15 is not None and len(df_m15) >= 20:
            adx_m15, pdi_m15, mdi_m15 = QuantEngine.adx(df_m15, 14)
            adx_v = float(adx_m15.iloc[-1])
            di_ok = (pdi_m15.iloc[-1] > mdi_m15.iloc[-1]) if signal_is_bull else (mdi_m15.iloc[-1] > pdi_m15.iloc[-1])
            if adx_v >= 25 and di_ok:
                score += 1
    except Exception:
        pass

    return score


# ----------------------------------------------------------------
#  SYSTÈME DE NOTATION  V15
# ----------------------------------------------------------------
def compute_score(flip_type, candles_ago,
                  mtf_pct, mtf_dominant,
                  zone_discount, zone_premium,
                  near_pdl, near_pdh, below_mid, above_mid,
                  in_bull_fvg, in_bear_fvg, fvg_near_bull, fvg_near_bear,
                  adx_val, pdi_val, mdi_val, atr_val, atr_mean,
                  midnight_bonus: bool = False):

    score = 0
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
            elif "429" in err_str:
                time.sleep(RETRY_DELAY * attempt * 3)
            elif "500" in err_str or "503" in err_str:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
            if attempt == MAX_RETRIES:
                return pd.DataFrame(), str(e)
    return pd.DataFrame(), "MAX_RETRIES"


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
#  FETCH STRENGTH
# ----------------------------------------------------------------
def fetch_strength_data(client) -> dict:
    dfs = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(fetch_oanda_data, client, pair, "H1", 100): pair
            for pair in STRENGTH_PAIRS
        }
        for fut in as_completed(futures):
            pair = futures[fut]
            df, err = fut.result()
            if not df.empty:
                dfs[pair] = df
    return dfs


# ----------------------------------------------------------------
#  ANALYSE PRINCIPALE
# ----------------------------------------------------------------
def analyze_asset(client, ticker, freshness_limit_min=30,
                  strength_scores=None, debug_log=None):

    def _reject(reason):
        if debug_log is not None:
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
                ex.submit(fetch_oanda_data, client, ticker, gran, cnt): gran
                for gran, cnt in fetch_specs
            }
            for fut in as_completed(futures):
                gran = futures[fut]
                fetch_results[gran] = fut.result()

        df_m15, err_m15 = fetch_results["M15"]
        df_h1,  _       = fetch_results["H1"]
        df_d,   err_d   = fetch_results["D"]
        df_4h,  _       = fetch_results["H4"]
        df_w,   _       = fetch_results["W"]
        df_mo,  _       = fetch_results["M"]

        if df_m15.empty: return _reject(f"FETCH_M15: {err_m15}")
        if df_d.empty:   return _reject(f"FETCH_D: {err_d}")

        price = df_m15['close'].iloc[-1]

        current_price_proxy = float(df_m15['close'].iloc[-1])
        bias, bias_detail = get_daily_bias_v2(df_d, current_price=current_price_proxy)
        bias_bull = bias in ("BULLISH", "STRONG BULLISH")
        bias_bear = bias in ("BEARISH", "STRONG BEARISH")

        dfs_mtf = {
            "M":   df_mo  if not (df_mo  is None or (hasattr(df_mo,  'empty') and df_mo.empty))  else None,
            "W":   df_w   if not (df_w   is None or (hasattr(df_w,   'empty') and df_w.empty))   else None,
            "D":   df_d   if not df_d.empty  else None,
            "4H":  df_4h  if not (df_4h  is None or (hasattr(df_4h,  'empty') and df_4h.empty))  else None,
            "1H":  df_h1  if not (df_h1  is None or (hasattr(df_h1,  'empty') and df_h1.empty))  else None,
            "15m": df_m15 if not df_m15.empty else None,
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

        if   (flip_type == "BULL" and bias_bull) or (flip_type == "BEAR" and bias_bear):
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
        atr_val  = atr_m15.iloc[-1]
        atr_mean = atr_m15.iloc[-50:].mean()

        in_bull_fvg, in_bear_fvg, nb_fvg, nr_fvg = detect_fvg(df_m15, price, lookback=80)
        fvg_near_bull = (nb_fvg is not None and
                         abs(price - (nb_fvg[0] + nb_fvg[1]) / 2) < atr_val * 1.0)
        fvg_near_bear = (nr_fvg is not None and
                         abs(price - (nr_fvg[0] + nr_fvg[1]) / 2) < atr_val * 1.0)

        # ══════════════════════════════════════════════════════════════
        #  MIDNIGHT OPEN — calculé EN PREMIER (avant la zone)
        #  Référence : 00h00 America/New_York (identique Pine Script TV)
        #  midnightSession = "0000-0001:23456"
        # ══════════════════════════════════════════════════════════════
        midnight_open = None
        try:
            ny_tz     = pytz.timezone("America/New_York")
            _m15_raw  = pd.to_datetime(df_m15.index)
            if _m15_raw.tz is None:
                m15_times = _m15_raw.tz_localize("UTC").tz_convert(ny_tz)
            else:
                m15_times = _m15_raw.tz_convert(ny_tz)
            today_ny = datetime.now(ny_tz).date()
            mn_mask  = (
                (m15_times.date == today_ny) &
                (m15_times.hour  == 0) &
                (m15_times.minute == 0)
            )
            mn_c = df_m15[mn_mask]
            if mn_c.empty:
                today_c = df_m15[m15_times.date == today_ny]
                if not today_c.empty:
                    midnight_open = float(today_c["open"].iloc[0])
            else:
                midnight_open = float(mn_c["open"].iloc[0])
        except Exception:
            pass

        # ══════════════════════════════════════════════════════════════
        #  ZONE DISCOUNT / PREMIUM — ICT pur
        #
        #  PDH  = df_d["high"].iloc[-2]  ←→  high[1] Daily Pine Script (ligne verte)
        #  PDL  = df_d["low"].iloc[-2]   ←→  low[1]  Daily Pine Script (ligne rouge)
        #  MO   = open M15 à 00:00 NY    ←→  midnightOpenPrice Pine Script (ligne jaune)
        #
        #  PREMIUM   = price > MO  ET  price ≤ PDH
        #  DISCOUNT  = price < MO  ET  price ≥ PDL
        #  EXT HIGH  = price > PDH  (hors range)
        #  EXT LOW   = price < PDL  (hors range)
        #
        #  Fallback si MO absent : midpoint PDH/PDL classique
        # ══════════════════════════════════════════════════════════════
        _d1_ref = -2 if len(df_d) >= 2 else -1
        pdh = float(df_d["high"].iloc[_d1_ref])   # PDH — ligne verte TV
        pdl = float(df_d["low"].iloc[_d1_ref])    # PDL — ligne rouge TV

        if midnight_open is not None:
            # Logique ICT pur avec MO comme équilibre intraday
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
            # Fallback : midpoint PDH/PDL si MO absent
            d1_mid        = (pdh + pdl) / 2.0
            in_discount   = price < d1_mid
            in_premium    = price > d1_mid
            zone_discount = in_discount
            zone_premium  = in_premium
            below_mid     = in_discount
            above_mid     = in_premium
            zone_label    = ("DISCOUNT" if in_discount else
                             "PREMIUM"  if in_premium  else "EQUILIBRE")

        # Bonus Midnight : confluence directionnelle avec MO
        midnight_bonus = False
        if midnight_open is not None:
            if flip_type == "BULL" and price < midnight_open:
                midnight_bonus = True
            elif flip_type == "BEAR" and price > midnight_open:
                midnight_bonus = True

        df_adx_src           = df_h1 if not (df_h1 is None or (hasattr(df_h1, 'empty') and df_h1.empty)) and len(df_h1) >= 20 else df_m15
        adx_s, pdi_s, mdi_s = QuantEngine.adx(df_adx_src, 14)
        adx_val_score = round(adx_s.iloc[-1], 1)
        pdi_val = round(pdi_s.iloc[-1], 1)
        mdi_val = round(mdi_s.iloc[-1], 1)

        # ADR (forex/metals) ou ATR (indices)
        if ticker in INDICES:
            _atr_d_raw  = QuantEngine.atr(df_d, 14).iloc[-1]
            adr_display = round(float(_atr_d_raw), 2) if not pd.isna(_atr_d_raw) else None
            adr_label   = "ATR"
        else:
            _adr_raw    = compute_adr(df_d, period=14)
            adr_display = round(float(_adr_raw), 5) if not pd.isna(_adr_raw) else None
            adr_label   = "ADR"

        score, grade, score_detail = compute_score(
            flip_type, candles_ago, mtf_pct, mtf_dominant,
            zone_discount, zone_premium,
            False, False,
            below_mid, above_mid,
            in_bull_fvg, in_bear_fvg, fvg_near_bull, fvg_near_bear,
            adx_val_score, pdi_val, mdi_val, atr_val, atr_mean,
            midnight_bonus=midnight_bonus
        )

        strength_delta = None
        if strength_scores:
            strength_delta = get_strength_delta(ticker, strength_scores)

        signal_is_bull = (flip_type == "BULL")
        _df_h4 = df_4h if not (df_4h is None or (hasattr(df_4h, 'empty') and df_4h.empty)) else None
        _df_h1 = df_h1 if not (df_h1 is None or (hasattr(df_h1, 'empty') and df_h1.empty)) else None
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
            "MTF Pct":        mtf_pct,
            "MTF Dom":        mtf_dominant,
            "MTF Details":    mtf_details,
            "Strength Δ":     strength_delta,
            "Momentum":       momentum,
            "signal_is_bull": signal_is_bull,
            "Midnight Bonus": midnight_bonus,
        }

    except Exception as e:
        if debug_log is not None:
            debug_log.append((ticker, f"EXCEPTION: {e}"))
        return None


# ----------------------------------------------------------------
#  INTERFACE STREAMLIT  (identique V15 original)
# ----------------------------------------------------------------
def main():
    st.set_page_config(
        page_title="BLUESTAR SNIPER V15",
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
          BLUESTAR SNIPER V15 {kz_html}
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
    env          = "practice"

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        freshness  = st.selectbox("Fraîcheur max du signal (min)", [15, 30, 45, 60], index=1)
    with col2:
        min_grade  = st.selectbox("Grade minimum", ["Tous", "B", "B+", "A", "A+"], index=0)
    with col3:
        show_debug = st.toggle("🐛 Debug", value=False)

    with st.expander("📘 Grille de notation V15"):
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

| Colonne | Description |
|---|---|
| **Zone** | DISCOUNT = prix sous MO (00h00 NY) · PREMIUM = prix dessus · EXT HIGH/LOW = hors PDH/PDL |
| **ADR / ATR** | ADR = Average Daily Range (forex/metals) · ATR = indices (US30, NAS100, DE30) |
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

    strength_scores = {}
    with st.spinner("Calcul Currency Strength H1…"):
        dfs_h1 = fetch_strength_data(client)
        strength_scores = compute_currency_strength(dfs_h1)

    with st.spinner("Analyse MTF en cours…"):
        for i, ticker in enumerate(assets):
            status.caption(f"⏳ {ticker}… ({i+1}/{len(assets)})")
            res = analyze_asset(
                client, ticker,
                freshness_limit_min=freshness,
                strength_scores=strength_scores,
                debug_log=debug_log
            )
            if res:
                results.append(res)
            time.sleep(0.15)
            progress.progress((i + 1) / len(assets))

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

    # ── TABLE HTML (identique V15 original) ──────────────────
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
        color = "#5a9e7a" if score >= 70 else "#9a7820" if score >= 40 else "#9e4a3a"
        width = max(4, score)
        return f"""<div style="display:flex;align-items:center;gap:8px">
          <div style="width:60px;height:4px;background:#1a1a22;border-radius:2px;overflow:hidden">
            <div style="width:{width}%;height:100%;background:{color};border-radius:2px"></div>
          </div>
          <span style="color:{color};font-weight:700;font-size:15px">{score}</span>
        </div>"""

    def adr_cell(v, label="ADR"):
        if v is None:
            return '<span style="color:#303040">—</span>'
        formatted = f"{v:.5f}" if v < 1.0 else f"{v:.2f}"
        tag_color = "#4a7898" if label == "ADR" else "#6a5a78"
        return (
            f'<span style="color:{tag_color};font-size:10px;font-family:\'IBM Plex Mono\','
            f'monospace;margin-right:4px">{label}</span>'
            f'<span style="color:#5a6880;font-family:\'IBM Plex Mono\','
            f'monospace;font-size:13px">{formatted}</span>'
        )

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
    active_kz = get_current_killzone()

    for _, row in df.iterrows():
        grade       = str(row.get("Grade", "C"))
        fresh       = "⚡" in str(row.get("Fraîcheur", ""))
        gs          = GRADE_STYLE.get(grade, GRADE_STYLE["C"])
        score       = int(row.get("Score /100", 0))
        ticker      = str(row.get("Actif + Note", "")).split("  ")[0].strip()
        sig         = str(row.get("Signal", "—"))
        adr_v       = row.get("ADR")
        adr_lbl     = str(row.get("ADR Label", "ADR"))
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

        kz_tag = (f'<span class="kz-badge">{killzone_badge(active_kz)}</span>'
                  if active_kz and fresh else "")

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
  <td>{adr_cell(adr_v, adr_lbl)}</td>
  <td>{force_cell(momentum, sdelta, is_bull)}</td>
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
