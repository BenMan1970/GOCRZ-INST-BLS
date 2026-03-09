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
#  BLUESTAR SNIPER V10  —  ICT SIGNAL ENGINE
# ================================================================

# ----------------------------------------------------------------
#  SYSTÈME DE NOTATION  (inspiré du scoring manuel ICT/SMC)
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

    signal_is_bull = (flip_type == "BULL")
    signal_is_bear = (flip_type == "BEAR")

    # ── BIAIS DAILY ──────────────────────────────────────────────
    bias_ok = (signal_is_bull and bias in ("BULLISH","STRONG BULLISH")) or \
              (signal_is_bear and bias in ("BEARISH","STRONG BEARISH"))
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
def _find_swing_points(series, wing=2):
    highs, lows = [], []
    for i in range(wing, len(series) - wing):
        window = series.iloc[i - wing: i + wing + 1]
        if series.iloc[i] == window.max():
            highs.append(i)
        if series.iloc[i] == window.min():
            lows.append(i)
    return highs, lows


def get_daily_bias(df_d):
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

        hh = last_sh > prev_sh
        hl = last_sl > prev_sl
        lh = last_sh < prev_sh
        ll = last_sl < prev_sl

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
    df_d_copy = df_d.copy()
    if df_d_copy.index.tz is not None:
        df_d_copy.index = df_d_copy.index.tz_convert('UTC')

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
    detail["Votes"] = f"{votes_bull}B / {votes_bear}S"

    if   votes_bull >= 4: bias = "STRONG BULLISH"
    elif votes_bull == 3: bias = "BULLISH"
    elif votes_bear >= 4: bias = "STRONG BEARISH"
    elif votes_bear == 3: bias = "BEARISH"
    else:                 bias = "NEUTRAL"

    return bias, detail


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
#  FETCH OANDA — avec retry + logs clairs
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
                return pd.DataFrame(), f"Réponse inattendue (pas de 'candles')"

            rows = [
                {"time":  pd.to_datetime(c["time"]),
                 "open":  float(c["mid"]["o"]),
                 "high":  float(c["mid"]["h"]),
                 "low":   float(c["mid"]["l"]),
                 "close": float(c["mid"]["c"])}
                for c in r.response["candles"] if c.get("complete")
            ]

            if not rows:
                return pd.DataFrame(), "Aucune bougie complète retournée"

            df = pd.DataFrame(rows).set_index("time")
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            return df, None

        except Exception as e:
            err_str = str(e)
            if "401" in err_str or "Unauthorized" in err_str:
                return pd.DataFrame(), "TOKEN_INVALID"
            elif "403" in err_str:
                return pd.DataFrame(), "TOKEN_FORBIDDEN (mauvais environment?)"
            elif "429" in err_str:
                wait = RETRY_DELAY * attempt * 3
                time.sleep(wait)
            elif "500" in err_str or "503" in err_str:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
            # Dernière tentative échouée
            if attempt == MAX_RETRIES:
                return pd.DataFrame(), str(e)

    return pd.DataFrame(), "MAX_RETRIES atteint"


# ----------------------------------------------------------------
#  TEST DE CONNEXION OANDA
# ----------------------------------------------------------------
def test_oanda_connection(client, account_id=None):
    """Retourne (ok: bool, message: str)"""
    try:
        if account_id:
            # Test précis avec l'account_id connu
            r = accounts.AccountDetails(account_id)
        else:
            r = accounts.AccountList()
        client.request(r)

        if account_id:
            acc = r.response.get("account", {})
            currency = acc.get("currency", "?")
            balance  = acc.get("balance", "?")
            return True, f"✅ Connecté — compte `{account_id}` · {currency} {balance}"
        else:
            accs = r.response.get("accounts", [])
            ids = [a.get("id","?") for a in accs]
            return True, f"✅ Connecté — {len(ids)} compte(s) : {', '.join(ids)}"
    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err:
            return False, "❌ Token invalide ou expiré"
        if "403" in err:
            return False, "❌ Accès refusé — vérifiez l'environment (practice vs live)"
        return False, f"❌ Erreur : {err}"


# ----------------------------------------------------------------
#  ANALYSE PRINCIPALE
# ----------------------------------------------------------------
def analyze_asset(client, ticker, freshness_limit_min=30, debug_log=None):
    """
    Retourne un dict de résultat ou None.
    debug_log : liste à laquelle on ajoute (ticker, raison_rejet) si fournie.
    """
    def _reject(reason):
        if debug_log is not None:
            debug_log.append((ticker, reason))
        return None

    try:
        df_d,   err_d   = fetch_oanda_data(client, ticker, "D",   100)
        df_m15, err_m15 = fetch_oanda_data(client, ticker, "M15", 400)
        df_h1,  err_h1  = fetch_oanda_data(client, ticker, "H1",  100)

        if df_d.empty:
            return _reject(f"FETCH_D_FAILED: {err_d}")
        if df_m15.empty:
            return _reject(f"FETCH_M15_FAILED: {err_m15}")

        price = df_m15['close'].iloc[-1]

        # ══ GATE 1 — BIAIS DAILY ═════════════════════════════════
        bias, bias_detail = get_daily_bias(df_d)
        bias_bull = bias in ("BULLISH", "STRONG BULLISH")
        bias_bear = bias in ("BEARISH", "STRONG BEARISH")
        if not bias_bull and not bias_bear:
            return _reject(f"NEUTRAL_BIAS ({bias_detail.get('Votes','?')})")

        # ══ GATE 2 — HMA 20 FLIP M15 ════════════════════════════
        hma = QuantEngine.hma(df_m15['close'], 20)
        if hma.isna().iloc[-5:].any():
            return _reject("HMA_NAN")

        flip_type, candles_ago = find_last_hma_flip(hma, max_lookback=10)

        if flip_type is None:
            return _reject("NO_HMA_FLIP")
        if flip_type == "BULL" and not bias_bull:
            return _reject(f"FLIP_BULL_VS_BIAS_{bias}")
        if flip_type == "BEAR" and not bias_bear:
            return _reject(f"FLIP_BEAR_VS_BIAS_{bias}")

        mins_ago = candles_ago * 15
        if mins_ago == 0:
            freshness_str = "⚡ < 15 min"
        elif mins_ago <= freshness_limit_min:
            freshness_str = f"⚡ {mins_ago} min"
        else:
            freshness_str = f"⏳ {mins_ago} min"

        signal_fresh = mins_ago <= freshness_limit_min

        # ══ GATE 3 — PRIX DU BON CÔTÉ DE LA HMA ════════════════
        hma_now = hma.iloc[-1]
        if flip_type == "BULL" and price <= hma_now:
            return _reject("PRICE_BELOW_HMA_ON_BULL")
        if flip_type == "BEAR" and price >= hma_now:
            return _reject("PRICE_ABOVE_HMA_ON_BEAR")

        # ══ GATE 4 — FVG DANS LE BON SENS ══════════════════════
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

        # ══ TOUS LES GATES PASSÉS — CALCUL SCORE ════════════════
        pdh = df_d['high'].iloc[-2]
        pdl = df_d['low'].iloc[-2]

        ny_tz    = pytz.timezone('America/New_York')
        df_m15_ny = df_m15.copy()
        df_m15_ny.index = df_m15_ny.index.tz_convert(ny_tz)
        today_ny = datetime.now(ny_tz).date()
        mask     = ((df_m15_ny.index.date == today_ny) &
                    (df_m15_ny.index.hour == 0) & (df_m15_ny.index.minute == 0))
        mid_c    = df_m15_ny[mask]
        m_open   = mid_c['open'].iloc[0] if not mid_c.empty else df_m15_ny['open'].iloc[0]

        atr_d         = QuantEngine.atr(df_d, 14).iloc[-1]
        below_mid     = price < m_open
        above_mid     = price > m_open
        near_pdl      = price <= (pdl + atr_d)
        near_pdh      = price >= (pdh - atr_d)
        zone_discount = below_mid and near_pdl
        zone_premium  = above_mid and near_pdh
        zone_label    = (
            "DISCOUNT" if zone_discount else
            "PREMIUM"  if zone_premium  else
            "NEUTRE"
        )

        df_adx_src           = df_h1 if not df_h1.empty and len(df_h1) >= 20 else df_m15
        adx_s, pdi_s, mdi_s  = QuantEngine.adx(df_adx_src, 14)
        adx_val = round(adx_s.iloc[-1], 1)
        pdi_val = round(pdi_s.iloc[-1], 1)
        mdi_val = round(mdi_s.iloc[-1], 1)

        score, grade, score_detail = compute_score(
            flip_type, candles_ago, bias,
            zone_discount, zone_premium,
            near_pdl, near_pdh,
            below_mid, above_mid,
            in_bull_fvg, in_bear_fvg,
            fvg_near_bull, fvg_near_bear,
            adx_val, pdi_val, mdi_val,
            atr_val, atr_mean
        )

        if signal_fresh:
            sig = "▲ LONG"  if flip_type == "BULL" else "▼ SHORT"
        else:
            sig = "LONG (expiré)" if flip_type == "BULL" else "SHORT (expiré)"

        return {
            "Actif + Note": ticker,
            "Signal":       sig,
            "Fraîcheur":    freshness_str,
            "Score /100":   score,
            "Grade":        grade,
            "Biais Daily":  bias,
            "Zone":         zone_label,
            "ADX H1":       adx_val,
            "Score Detail": score_detail,
        }

    except Exception as e:
        if debug_log is not None:
            debug_log.append((ticker, f"EXCEPTION: {e}"))
        return None


# ----------------------------------------------------------------
#  INTERFACE
# ----------------------------------------------------------------
def main():
    st.set_page_config(
        page_title="BLUESTAR SNIPER V10",
        layout="wide",
        initial_sidebar_state="collapsed"
    )

    # ── CSS global ────────────────────────────────────────────────
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Space+Grotesk:wght@400;600;700&display=swap');

    html, body, [class*="css"] { font-family: 'Space Grotesk', sans-serif; }
    .stApp { background: #0e0e14; }

    /* Titres */
    h1 { font-family: 'IBM Plex Mono', monospace !important; letter-spacing: .04em; }

    /* Boutons */
    .stButton > button {
        background: #141420 !important;
        color: #a0b4cc !important;
        border: 1px solid #2a3448 !important;
        font-family: 'IBM Plex Mono', monospace !important;
        font-weight: 700 !important;
        letter-spacing: .06em !important;
        border-radius: 4px !important;
        transition: all .2s ease !important;
    }
    .stButton > button:hover {
        background: #1c2030 !important;
        border-color: #3a4a68 !important;
        color: #c8d8e8 !important;
    }

    /* Selectbox */
    .stSelectbox label { color: #606080 !important; font-size: 12px !important; text-transform: uppercase !important; letter-spacing: .08em !important; }

    /* Metric cards */
    [data-testid="stMetric"] {
        background: #121218;
        border: 1px solid #1e1e2e;
        border-radius: 6px;
        padding: 12px 16px !important;
    }
    [data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace !important; }

    /* Progress bar */
    .stProgress > div > div { background: #3a7a58 !important; }

    /* Expander */
    .streamlit-expanderHeader { color: #606080 !important; font-size: 12px !important; text-transform: uppercase !important; }

    /* Divider */
    hr { border-color: #1a1a28 !important; }

    /* Debug box */
    .debug-box {
        background: #121218;
        border: 1px solid #1e1e2e;
        border-radius: 6px;
        padding: 12px 16px;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 11px;
        color: #505070;
        margin-top: 8px;
    }
    </style>
    """, unsafe_allow_html=True)

    # ── HEADER ─────────────────────────────────────────────────────
    st.markdown("""
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:4px">
      <span style="font-size:36px">🎯</span>
      <div>
        <h1 style="margin:0;font-size:28px;color:#e8e8f8">BLUESTAR SNIPER V10</h1>
        <p style="margin:0;color:#4a4a7a;font-family:'IBM Plex Mono',monospace;font-size:12px;letter-spacing:.1em">
          HMA 20 M15 · SCORE /100 · GRADE A+ → C · ICT / SMC ENGINE
        </p>
      </div>
    </div>
    <hr>
    """, unsafe_allow_html=True)

    # ── VÉRIF TOKEN ────────────────────────────────────────────────
    missing = [k for k in ("OANDA_ACCESS_TOKEN", "OANDA_ACCOUNT_ID") if k not in st.secrets]
    if missing:
        st.error(f"🔑 **Secret(s) manquant(s) :** `{'`, `'.join(missing)}`")
        st.code("""# Settings → Secrets (format attendu)
OANDA_ACCESS_TOKEN = "ton-token-ici"
OANDA_ACCOUNT_ID   = "ton-account-id"
""", language="toml")
        st.stop()

    ACCESS_TOKEN = st.secrets["OANDA_ACCESS_TOKEN"]
    ACCOUNT_ID   = st.secrets["OANDA_ACCOUNT_ID"]
    env          = "practice"   # change en "live" si token live

    # ── CONTRÔLES ─────────────────────────────────────────────────
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        freshness = st.selectbox("Fraîcheur max du signal (min)", [15, 30, 45, 60], index=1)
    with col2:
        min_grade = st.selectbox("Grade minimum", ["Tous", "B", "B+", "A", "A+"], index=0)
    with col3:
        show_debug = st.toggle("🐛 Debug", value=False)


    with st.expander("📘 Grille de notation"):
        st.markdown("""
| Critère | Max | Détail |
|---|---|---|
| **Trigger HMA flip** | 30 pts | ≤15 min = 30 · ≤30 min = 20 · ≤45 min = 10 · >45 min = 0 |
| **Biais Daily** | 20 pts | Concordant = 20 · Opposé = 0 |
| **Zone d'intérêt** | 20 pts | Discount+PDL ou Premium+PDH = 20 · Zone seule = 10 · Hors zone = 0 |
| **FVG M15** | 15 pts | Dans le FVG = 15 · FVG proche = 7 · Absent = 0 |
| **ADX momentum** | 10 pts | ADX>25+DI ok = 10 · ADX>20+DI ok = 6 · ADX>20 seul = 3 |
| **ATR actif** | 5 pts | ≥ moyenne = 5 · ≥ 50% = 3 · Plat = 0 |

| Grade | Score | Signal |
|---|---|---|
| 💎 **A+** | 85-100 | Tout aligné — trader immédiatement |
| 🥇 **A** | 70-84 | Très bon — 1 élément mineur manque |
| 🥈 **B+** | 55-69 | En formation — surveiller |
| 🔵 **B** | 40-54 | Partiel — attendre confirmation |
| ⚪ **C** | < 40 | Faible — ignorer |
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

    # ── INIT CLIENT ───────────────────────────────────────────────
    client = oandapyV20.API(
        access_token=ACCESS_TOKEN,
        environment=env
    )

    assets = [
        # Majeurs
        "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
        "AUD_USD", "USD_CAD", "NZD_USD",
        # Croisés EUR
        "EUR_GBP", "EUR_JPY", "EUR_CHF",
        "EUR_AUD", "EUR_CAD", "EUR_NZD",
        # Croisés GBP
        "GBP_JPY", "GBP_CHF", "GBP_AUD",
        "GBP_CAD", "GBP_NZD",
        # Croisés JPY
        "AUD_JPY", "CAD_JPY", "CHF_JPY", "NZD_JPY",
        # Mineurs
        "AUD_CAD", "AUD_CHF", "AUD_NZD",
        "CAD_CHF", "NZD_CAD", "NZD_CHF",
        # Métaux
        "XAU_USD", "XAG_USD",
        # Indices
        "US30_USD", "NAS100_USD", "DE30_EUR",
    ]

    results   = []
    debug_log = []
    progress  = st.progress(0)
    status    = st.empty()
    t_start   = time.time()

    with st.spinner("Analyse en cours…"):
        for i, ticker in enumerate(assets):
            status.caption(f"⏳ Analyse {ticker}… ({i+1}/{len(assets)})")
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

    # ── DEBUG LOG ─────────────────────────────────────────────────
    if show_debug:
        with st.expander(f"🐛 Debug — {len(debug_log)} actifs rejetés en {elapsed}s"):
            if debug_log:
                counts = Counter(r for _, r in debug_log)
                cols_d = st.columns(2)
                with cols_d[0]:
                    st.markdown("**Raisons de rejet (agrégées)**")
                    # Group by category
                    cats = Counter()
                    for _, reason in debug_log:
                        cat = reason.split("_")[0] if "_" in reason else reason[:20]
                        cats[cat] += 1
                    for cat, n in cats.most_common():
                        st.markdown(f"- `{cat}` → **{n}**")
                with cols_d[1]:
                    st.markdown("**Détail par actif**")
                    for ticker, reason in debug_log[:30]:
                        st.markdown(f"`{ticker}` — {reason}")
                    if len(debug_log) > 30:
                        st.caption(f"… et {len(debug_log)-30} autres")
            else:
                st.success("Aucun rejet — tous les actifs ont passé les gates !")

    # ── PAS DE RÉSULTATS ─────────────────────────────────────────
    if not results:
        st.warning("**Aucun signal valide détecté** sur cette session.")
        counts = Counter(r for _, r in debug_log)
        top_reasons = counts.most_common(5)
        if top_reasons:
            st.markdown("**Top raisons de rejet :**")
            for reason, n in top_reasons:
                icon = "📡" if "FETCH" in reason else "📊" if "BIAS" in reason else "🔄" if "HMA" in reason else "📐" if "FVG" in reason else "⚙️"
                st.markdown(f"{icon} `{reason}` → **{n}** actif(s)")

            # Diagnostic automatique
            fetch_fails = sum(n for r, n in top_reasons if "FETCH" in r or "TOKEN" in r)
            if fetch_fails > len(assets) * 0.5:
                st.error("""🔴 **Diagnostic : Problème de connexion OANDA**  
Plus de 50 % des actifs échouent au fetch.  
→ Vérifie que `OANDA_ACCESS_TOKEN` et `OANDA_ACCOUNT_ID` sont corrects dans les secrets  
→ Vérifie que la variable `env` dans le code correspond à ton type de token (`practice` ou `live`)""")
        return

    df = pd.DataFrame(results)

    # ── FILTRE GRADE MIN ──────────────────────────────────────────
    grade_order = {"A+": 5, "A": 4, "B+": 3, "B": 2, "C": 1}
    min_map     = {"Tous": 0, "B": 2, "B+": 3, "A": 4, "A+": 5}
    min_val     = min_map[min_grade]
    df = df[df["Grade"].map(grade_order) >= min_val]

    if df.empty:
        st.info(f"Aucun signal de grade ≥ **{min_grade}** — élargis le filtre.")
        return

    # ── TRI ───────────────────────────────────────────────────────
    def sort_key(row):
        fresh = 1 if "⚡" in str(row["Fraîcheur"]) else 0
        g     = grade_order.get(row["Grade"], 0)
        s     = row["Score /100"]
        return (-fresh, -g, -s)

    df["_sk"] = df.apply(sort_key, axis=1)
    df = df.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)

    # ── MÉTRIQUES RAPIDES ─────────────────────────────────────────
    a_plus  = len(df[(df["Grade"] == "A+") & df["Fraîcheur"].str.contains("⚡", na=False)])
    a_grade = len(df[(df["Grade"] == "A")  & df["Fraîcheur"].str.contains("⚡", na=False)])
    b_watch = len(df[df["Grade"].isin(["B+","B"]) & df["Fraîcheur"].str.contains("⚡", na=False)])
    longs   = len(df[df["Signal"].str.contains("LONG", na=False) & ~df["Signal"].str.contains("expiré")])
    shorts  = len(df[df["Signal"].str.contains("SHORT", na=False) & ~df["Signal"].str.contains("expiré")])

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
            return "color:#5a9e7a;font-weight:700;font-size:15px;letter-spacing:.03em"
        if "SHORT" in s and "expiré" not in s:
            return "color:#9e4a3a;font-weight:700;font-size:15px;letter-spacing:.03em"
        return "color:#303040;font-size:12px"

    def adx_style(v):
        try:
            f = float(v)
            if f >= 25: return "color:#5a9e7a;font-weight:700"
            if f >= 20: return "color:#9a7820;font-weight:700"
            return "color:#404055"
        except: return "color:#333"

    def zone_style(z):
        if "DISCOUNT" in z: return "color:#4a7898;font-weight:600"
        if "PREMIUM"  in z: return "color:#8a6028;font-weight:600"
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
          <span style="color:{color};font-weight:700;font-size:13px">{score}</span>
        </div>"""

    html = """
<style>
.sc-wrap{overflow-x:auto;margin-top:4px}
.sc-tbl{width:100%;border-collapse:collapse;font-family:'IBM Plex Mono','Courier New',monospace;font-size:13px}
.sc-tbl thead tr{border-bottom:2px solid #1e1e3a}
.sc-tbl th{
  padding:10px 16px;text-align:left;color:#3a3a6a;
  font-size:10px;text-transform:uppercase;letter-spacing:.12em;font-weight:600;white-space:nowrap
}
.sc-tbl td{padding:13px 16px;border-bottom:1px solid #0f0f1e;vertical-align:middle}
.sc-tbl tr:hover td{background:#111118 !important}
.ticker{font-size:20px;font-weight:800;color:#c8c8d8;letter-spacing:.04em;white-space:nowrap;font-family:'IBM Plex Mono',monospace}
.bias-tag{font-size:11px;font-weight:700;letter-spacing:.07em;vertical-align:middle;margin-left:8px;text-transform:uppercase;opacity:.85}
.grade-pill{
  display:inline-block;padding:2px 8px;border-radius:2px;
  font-size:11px;font-weight:700;letter-spacing:.08em;
  border:1px solid currentColor;margin-left:10px;vertical-align:middle;font-family:'IBM Plex Mono',monospace
}
</style>
<div class="sc-wrap"><table class="sc-tbl">
<thead><tr>
  <th style="width:90px">Fraîcheur</th>
  <th>Actif</th>
  <th>Signal</th>
  <th>Zone</th>
  <th>Score /100</th>
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
        fresh_str = str(row.get("Fraîcheur","—"))

        bull_bias = "BULLISH" in bias
        bias_icon = "▲" if bull_bias else "▼"
        bias_lbl  = ("STRONG BULL" if bias == "STRONG BULLISH"
                     else "BULL"       if bias == "BULLISH"
                     else "STRONG BEAR" if bias == "STRONG BEARISH"
                     else "BEAR")
        bias_color = "#5a9e7a" if bull_bias else "#9e4a3a"

        row_bg = gs["bg"] if fresh and grade in ("A+","A") else "transparent"

        html += f"""<tr style="background:{row_bg}">
  <td style="{fresh_style(fresh_str)}">{fresh_str}</td>
  <td style="white-space:nowrap">
    <span class="ticker">{ticker}</span>
    <span class="bias-tag" style="color:{bias_color}">{bias_icon} {bias_lbl}</span>
    <span class="grade-pill" style="color:{gs['color']}">{gs['label']}</span>
  </td>
  <td style="{sig_style(sig)}">{sig}</td>
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
    </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
