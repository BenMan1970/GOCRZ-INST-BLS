import streamlit as st
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime
import pytz
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================================================================
#  BLUESTAR ZONE INDICATOR — PDH / MIDNIGHT OPEN / PDL
#  Visualisation des zones PREMIUM / DISCOUNT (ICT pur)
#  Compatible 100% avec l'indicateur TradingView :
#    PDH  = ligne VERTE  = df_d["high"].iloc[-2]
#    MO   = ligne JAUNE  = open M15 à 00:00 NY
#    PDL  = ligne ROUGE  = df_d["low"].iloc[-2]
# ================================================================

ASSETS = [
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

MAX_RETRIES = 3
RETRY_DELAY = 1.5


# ----------------------------------------------------------------
#  FETCH OANDA
# ----------------------------------------------------------------
def fetch_oanda(client, instrument, granularity, count):
    for attempt in range(1, MAX_RETRIES + 1):
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
                for c in r.response.get("candles", []) if c.get("complete")
            ]
            if not rows:
                return pd.DataFrame(), "Aucune bougie"
            df = pd.DataFrame(rows).set_index("time")
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            return df, None
        except Exception as e:
            if attempt == MAX_RETRIES:
                return pd.DataFrame(), str(e)
            time.sleep(RETRY_DELAY * attempt)
    return pd.DataFrame(), "MAX_RETRIES"


# ----------------------------------------------------------------
#  CALCUL DES NIVEAUX CLÉS
# ----------------------------------------------------------------
def compute_levels(df_d, df_m15):
    """
    Retourne pdh, pdl, midnight_open, price, zone_label
    Logique identique au Pine Script TradingView :
      PDH  = high[1]  daily  → iloc[-2]
      PDL  = low[1]   daily  → iloc[-2]
      MO   = open bougie M15 à 00:00 America/New_York
    """
    if df_d is None or len(df_d) < 2 or df_m15 is None or df_m15.empty:
        return None

    pdh   = float(df_d["high"].iloc[-2])
    pdl   = float(df_d["low"].iloc[-2])
    price = float(df_m15["close"].iloc[-1])

    # ── Midnight Open (00:00 NY) ──────────────────────────────
    midnight_open = None
    try:
        ny_tz     = pytz.timezone("America/New_York")
        m15_idx   = df_m15.index
        if m15_idx.tz is None:
            m15_times = m15_idx.tz_localize("UTC").tz_convert(ny_tz)
        else:
            m15_times = m15_idx.tz_convert(ny_tz)

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

    # ── Classification de zone ────────────────────────────────
    if midnight_open is not None:
        if price > pdh:
            zone = "EXT HIGH"
        elif price < pdl:
            zone = "EXT LOW"
        elif price > midnight_open:
            zone = "PREMIUM"
        elif price < midnight_open:
            zone = "DISCOUNT"
        else:
            zone = "EQUILIBRE"
    else:
        d1_mid = (pdh + pdl) / 2.0
        if price > pdh:
            zone = "EXT HIGH"
        elif price < pdl:
            zone = "EXT LOW"
        elif price > d1_mid:
            zone = "PREMIUM"
        elif price < d1_mid:
            zone = "DISCOUNT"
        else:
            zone = "EQUILIBRE"

    return {
        "pdh":          pdh,
        "pdl":          pdl,
        "midnight_open": midnight_open,
        "price":        price,
        "zone":         zone,
        "mo_fallback":  midnight_open is None,
    }


# ----------------------------------------------------------------
#  RENDU : BARRE VERTICALE DE ZONE (SVG-like en HTML/CSS)
# ----------------------------------------------------------------
def render_zone_bar(levels: dict, ticker: str) -> str:
    """
    Génère un bloc HTML affichant la barre verticale PDH→MO→PDL
    avec le prix courant positionné dessus.
    """
    pdh   = levels["pdh"]
    pdl   = levels["pdl"]
    mo    = levels["midnight_open"]
    price = levels["price"]
    zone  = levels["zone"]
    fallback = levels["mo_fallback"]

    total_range = pdh - pdl
    if total_range <= 0:
        return ""

    # Pivot : si MO disponible, utiliser MO; sinon midpoint
    pivot = mo if mo is not None else (pdh + pdl) / 2.0

    # Pct position (0% = PDL, 100% = PDH)
    def pct(val):
        v = (val - pdl) / total_range * 100
        return max(0.0, min(100.0, v))

    pivot_pct  = pct(pivot)
    price_pct  = pct(price)
    price_clamp = max(2.0, min(98.0, price_pct))

    # ── Couleurs zones ────────────────────────────────────────
    ZONE_COLORS = {
        "PREMIUM":   {"bg": "rgba(138, 96, 40, 0.22)", "text": "#c8952a", "icon": "▲"},
        "DISCOUNT":  {"bg": "rgba(74, 120, 152, 0.22)", "text": "#4a98c8", "icon": "▼"},
        "EXT HIGH":  {"bg": "rgba(158, 74, 58, 0.18)", "text": "#e05a3a", "icon": "⚡"},
        "EXT LOW":   {"bg": "rgba(58, 122, 158, 0.18)", "text": "#3a9ece", "icon": "⚡"},
        "EQUILIBRE": {"bg": "rgba(90,90,90,0.15)",      "text": "#909090", "icon": "—"},
    }
    zc = ZONE_COLORS.get(zone, ZONE_COLORS["EQUILIBRE"])

    # ── Formatage du prix (auto-détection décimales) ──────────
    def fmt(v):
        if v is None:
            return "—"
        if v > 1000:
            return f"{v:,.2f}"
        elif v > 10:
            return f"{v:.3f}"
        else:
            return f"{v:.5f}"

    # ── Label ticker court ────────────────────────────────────
    short = ticker.replace("_USD", "").replace("_EUR", "").replace("_", "/")

    # ── Hauteur totale de la barre en px ─────────────────────
    BAR_H = 200

    # position pixel du pivot et du prix (inversion: 100% = haut)
    pivot_px   = BAR_H * (1 - pivot_pct  / 100)
    price_px   = BAR_H * (1 - price_clamp / 100)

    # Heights des deux zones (PREMIUM = pivot→top, DISCOUNT = bottom→pivot)
    premium_h  = pivot_px         # px depuis le haut
    discount_h = BAR_H - pivot_px # px depuis pivot jusqu'en bas

    # Label fallback MO
    mo_label_extra = (' <span style="font-size:9px;color:#606060">(fallback)</span>'
                      if fallback else "")

    # ── Pct de remplissage dans la zone (pour mini-gauge) ─────
    if zone == "PREMIUM" and pivot is not None:
        fill_pct = (price - pivot) / (pdh - pivot) * 100 if pdh > pivot else 50
    elif zone == "DISCOUNT" and pivot is not None:
        fill_pct = (pivot - price) / (pivot - pdl) * 100 if pivot > pdl else 50
    else:
        fill_pct = 50
    fill_pct = max(0, min(100, fill_pct))

    html = f"""
<div style="
    background:#0c0c14;
    border:1px solid #1a1a2e;
    border-top:2px solid {zc['text']}33;
    border-radius:6px;
    padding:14px 16px 16px;
    font-family:'IBM Plex Mono',monospace;
    min-width:180px;
    position:relative;
    overflow:hidden;
">
  <!-- Fond glow ambiant -->
  <div style="
    position:absolute;top:0;left:0;right:0;bottom:0;
    background:radial-gradient(ellipse at 50% 0%, {zc['text']}08 0%, transparent 70%);
    pointer-events:none;
  "></div>

  <!-- Header ticker -->
  <div style="
    display:flex;justify-content:space-between;align-items:center;
    margin-bottom:12px;position:relative;
  ">
    <span style="
      font-size:16px;font-weight:800;color:#c0c0d8;
      letter-spacing:.06em;
    ">{short}</span>
    <span style="
      font-size:11px;font-weight:700;color:{zc['text']};
      background:{zc['bg']};
      padding:2px 8px;border-radius:2px;
      letter-spacing:.1em;
    ">{zc['icon']} {zone}</span>
  </div>

  <!-- Corps : barre verticale + labels -->
  <div style="display:flex;gap:12px;align-items:stretch;position:relative;">

    <!-- Barre verticale -->
    <div style="
      position:relative;width:22px;flex-shrink:0;
      height:{BAR_H}px;
    ">
      <!-- Zone PREMIUM (PDH → MO) -->
      <div style="
        position:absolute;top:0;left:0;right:0;
        height:{premium_h:.1f}px;
        background:rgba(138,96,40,0.18);
        border-left:3px solid #c8952a44;
      "></div>
      <!-- Zone DISCOUNT (MO → PDL) -->
      <div style="
        position:absolute;left:0;right:0;bottom:0;
        height:{discount_h:.1f}px;
        background:rgba(74,120,152,0.18);
        border-left:3px solid #4a98c844;
      "></div>

      <!-- Ligne PDH (verte) -->
      <div style="
        position:absolute;top:0px;left:-4px;right:-4px;
        height:2px;background:#39FF14;
        box-shadow:0 0 6px #39FF1466;
      "></div>
      <!-- Ligne PDL (rouge) -->
      <div style="
        position:absolute;bottom:0px;left:-4px;right:-4px;
        height:2px;background:#FF003C;
        box-shadow:0 0 6px #FF003C66;
      "></div>
      <!-- Ligne MO (jaune) -->
      <div style="
        position:absolute;left:-4px;right:-4px;
        top:{pivot_px:.1f}px;
        height:2px;background:#FFE000;
        box-shadow:0 0 6px #FFE00066;
      "></div>

      <!-- Prix courant (triangle) -->
      <div style="
        position:absolute;
        top:{price_px - 5:.1f}px;
        left:26px;
        width:0;height:0;
        border-top:5px solid transparent;
        border-bottom:5px solid transparent;
        border-right:8px solid {zc['text']};
        filter:drop-shadow(0 0 4px {zc['text']}88);
      "></div>
    </div>

    <!-- Labels niveaux -->
    <div style="
      position:relative;
      flex:1;height:{BAR_H}px;
      font-size:11px;
    ">
      <!-- PDH -->
      <div style="
        position:absolute;top:-2px;left:0;right:0;
        display:flex;justify-content:space-between;align-items:center;
      ">
        <span style="color:#39FF14;font-weight:700;font-size:10px;letter-spacing:.08em">PDH</span>
        <span style="color:#39FF14;font-size:10px">{fmt(pdh)}</span>
      </div>

      <!-- Label PREMIUM centré dans la zone -->
      <div style="
        position:absolute;
        top:{premium_h/2 - 8:.1f}px;
        left:0;right:0;text-align:center;
      ">
        <span style="color:#c8952a55;font-size:9px;letter-spacing:.14em;text-transform:uppercase">PREMIUM</span>
      </div>

      <!-- MO -->
      <div style="
        position:absolute;top:{pivot_px - 8:.1f}px;left:0;right:0;
        display:flex;justify-content:space-between;align-items:center;
      ">
        <span style="color:#FFE000;font-weight:700;font-size:10px;letter-spacing:.08em">
          MO{mo_label_extra}
        </span>
        <span style="color:#FFE000;font-size:10px">{fmt(mo)}</span>
      </div>

      <!-- Label DISCOUNT centré dans la zone -->
      <div style="
        position:absolute;
        top:{pivot_px + discount_h/2 - 8:.1f}px;
        left:0;right:0;text-align:center;
      ">
        <span style="color:#4a98c855;font-size:9px;letter-spacing:.14em;text-transform:uppercase">DISCOUNT</span>
      </div>

      <!-- PDL -->
      <div style="
        position:absolute;bottom:-2px;left:0;right:0;
        display:flex;justify-content:space-between;align-items:center;
      ">
        <span style="color:#FF003C;font-weight:700;font-size:10px;letter-spacing:.08em">PDL</span>
        <span style="color:#FF003C;font-size:10px">{fmt(pdl)}</span>
      </div>

      <!-- Prix courant -->
      <div style="
        position:absolute;
        top:{price_px - 9:.1f}px;
        left:0;right:0;
        display:flex;justify-content:space-between;align-items:center;
      ">
        <span style="color:{zc['text']};font-weight:700;font-size:10px;letter-spacing:.06em">PRICE</span>
        <span style="color:{zc['text']};font-weight:700;font-size:11px">{fmt(price)}</span>
      </div>
    </div>
  </div>

  <!-- Footer : mini-gauge de position dans la zone -->
  <div style="margin-top:14px;position:relative;">
    <div style="
      display:flex;justify-content:space-between;
      font-size:9px;color:#303048;letter-spacing:.1em;
      margin-bottom:3px;
    ">
      <span>{'PDL' if zone in ('DISCOUNT','EXT LOW')  else 'MO'}</span>
      <span style="color:{zc['text']};font-size:9px">
        POSITION DANS LA ZONE
      </span>
      <span>{'MO'  if zone in ('DISCOUNT','EXT LOW')  else 'PDH'}</span>
    </div>
    <div style="
      height:3px;background:#111122;border-radius:2px;overflow:hidden;
    ">
      <div style="
        height:100%;width:{fill_pct:.1f}%;
        background:linear-gradient(90deg, {zc['text']}66, {zc['text']});
        border-radius:2px;
        transition:width .3s ease;
      "></div>
    </div>
  </div>
</div>
"""
    return html


# ----------------------------------------------------------------
#  RENDU : RÉSUMÉ GLOBAL (grille complète)
# ----------------------------------------------------------------
def render_summary_table(all_levels: list) -> str:
    """
    Tableau compact listant tous les actifs avec leur zone.
    """
    ZONE_COLORS = {
        "PREMIUM":   "#c8952a",
        "DISCOUNT":  "#4a98c8",
        "EXT HIGH":  "#e05a3a",
        "EXT LOW":   "#3a9ece",
        "EQUILIBRE": "#707070",
        "ERROR":     "#404040",
    }

    rows_html = ""
    for item in all_levels:
        ticker = item["ticker"]
        short  = ticker.replace("_USD","").replace("_EUR","").replace("_","<br>")
        if item.get("error"):
            rows_html += f"""
<tr>
  <td style="color:#303048;font-size:11px;padding:6px 8px;border-bottom:1px solid #0f0f1e">
    {short}
  </td>
  <td colspan="4" style="color:#303048;font-size:10px;padding:6px 8px;border-bottom:1px solid #0f0f1e">
    — données indisponibles
  </td>
</tr>"""
            continue

        lv    = item["levels"]
        zone  = lv["zone"]
        zcolor = ZONE_COLORS.get(zone, "#707070")
        mo_ind = "" if not lv["mo_fallback"] else '<span style="color:#404060;font-size:9px"> ⚠</span>'

        def fmt(v):
            if v is None: return "—"
            if v > 1000:  return f"{v:,.0f}"
            elif v > 10:  return f"{v:.3f}"
            else:         return f"{v:.5f}"

        rows_html += f"""
<tr style="transition:background .15s" onmouseover="this.style.background='#111118'" onmouseout="this.style.background='transparent'">
  <td style="
    color:#a0a0b8;font-size:12px;font-weight:700;
    padding:8px 10px;border-bottom:1px solid #0f0f1e;
    letter-spacing:.06em;white-space:nowrap;
  ">{ticker.replace("_","/")}</td>
  <td style="
    padding:8px 10px;border-bottom:1px solid #0f0f1e;
    color:#39FF14;font-size:11px;font-family:'IBM Plex Mono',monospace;
  ">{fmt(lv['pdh'])}</td>
  <td style="
    padding:8px 10px;border-bottom:1px solid #0f0f1e;
    color:#FFE000;font-size:11px;font-family:'IBM Plex Mono',monospace;
  ">{fmt(lv['midnight_open'])}{mo_ind}</td>
  <td style="
    padding:8px 10px;border-bottom:1px solid #0f0f1e;
    color:#FF003C;font-size:11px;font-family:'IBM Plex Mono',monospace;
  ">{fmt(lv['pdl'])}</td>
  <td style="
    padding:8px 10px;border-bottom:1px solid #0f0f1e;
    color:#a0a0b8;font-size:11px;font-family:'IBM Plex Mono',monospace;
  ">{fmt(lv['price'])}</td>
  <td style="padding:8px 10px;border-bottom:1px solid #0f0f1e;">
    <span style="
      color:{zcolor};font-weight:700;font-size:11px;
      background:{zcolor}18;
      padding:2px 8px;border-radius:2px;
      letter-spacing:.1em;
      font-family:'IBM Plex Mono',monospace;
    ">{zone}</span>
  </td>
</tr>"""

    # Stats globales
    zones_all = [i["levels"]["zone"] for i in all_levels if not i.get("error")]
    from collections import Counter
    cnt = Counter(zones_all)
    total = len(zones_all) or 1

    def bar(n):
        w = int(n / total * 80)
        return f'<div style="width:{w}px;height:6px;background:currentColor;border-radius:2px;display:inline-block"></div>'

    stats_html = f"""
<div style="
  display:flex;gap:24px;flex-wrap:wrap;
  font-family:'IBM Plex Mono',monospace;
  font-size:11px;padding:14px 16px;
  background:#0a0a12;border-radius:4px;
  margin-bottom:16px;
">
  <div style="color:#c8952a">
    {bar(cnt.get('PREMIUM',0))} PREMIUM <strong>{cnt.get('PREMIUM',0)}</strong>
  </div>
  <div style="color:#4a98c8">
    {bar(cnt.get('DISCOUNT',0))} DISCOUNT <strong>{cnt.get('DISCOUNT',0)}</strong>
  </div>
  <div style="color:#e05a3a">
    {bar(cnt.get('EXT HIGH',0))} EXT HIGH <strong>{cnt.get('EXT HIGH',0)}</strong>
  </div>
  <div style="color:#3a9ece">
    {bar(cnt.get('EXT LOW',0))} EXT LOW <strong>{cnt.get('EXT LOW',0)}</strong>
  </div>
  <div style="color:#909090">
    {bar(cnt.get('EQUILIBRE',0))} EQUILIBRE <strong>{cnt.get('EQUILIBRE',0)}</strong>
  </div>
</div>"""

    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&display=swap');
</style>
{stats_html}
<div style="overflow-x:auto">
<table style="
  width:100%;border-collapse:collapse;
  font-family:'IBM Plex Mono',monospace;
  background:#0a0a12;border-radius:6px;
  overflow:hidden;
">
<thead>
<tr style="border-bottom:2px solid #2a2a5a">
  <th style="padding:10px 10px;text-align:left;color:#404060;font-size:11px;letter-spacing:.14em;text-transform:uppercase;font-weight:700;">Actif</th>
  <th style="padding:10px 10px;text-align:left;color:#39FF14;font-size:11px;letter-spacing:.1em;">PDH 🟢</th>
  <th style="padding:10px 10px;text-align:left;color:#FFE000;font-size:11px;letter-spacing:.1em;">MO 🟡</th>
  <th style="padding:10px 10px;text-align:left;color:#FF003C;font-size:11px;letter-spacing:.1em;">PDL 🔴</th>
  <th style="padding:10px 10px;text-align:left;color:#606080;font-size:11px;letter-spacing:.1em;">Prix</th>
  <th style="padding:10px 10px;text-align:left;color:#606080;font-size:11px;letter-spacing:.1em;">Zone</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</div>"""


# ----------------------------------------------------------------
#  FETCH PARALLÈLE TOUS LES ACTIFS
# ----------------------------------------------------------------
def fetch_all_levels(client, assets, progress_cb=None):
    results = []

    def _fetch_one(ticker):
        df_d,   _ = fetch_oanda(client, ticker, "D",   50)
        df_m15, _ = fetch_oanda(client, ticker, "M15", 200)
        if df_d.empty or df_m15.empty:
            return {"ticker": ticker, "error": True}
        lv = compute_levels(df_d, df_m15)
        if lv is None:
            return {"ticker": ticker, "error": True}
        return {"ticker": ticker, "levels": lv, "error": False}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_one, t): t for t in assets}
        done = 0
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if progress_cb:
                progress_cb(done / len(assets))

    results.sort(key=lambda x: assets.index(x["ticker"]))
    return results


# ----------------------------------------------------------------
#  MAIN STREAMLIT
# ----------------------------------------------------------------
def main():
    st.set_page_config(
        page_title="BLUESTAR ZONE INDICATOR",
        layout="wide",
        initial_sidebar_state="collapsed"
    )

    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Space+Grotesk:wght@400;600;700&display=swap');
    html, body, [class*="css"] { font-family:'Space Grotesk',sans-serif; }
    .stApp { background:#0e0e14; }
    h1 { font-family:'IBM Plex Mono',monospace !important; }
    .stButton > button {
        background:#141420 !important; color:#a0b4cc !important;
        border:1px solid #2a3448 !important;
        font-family:'IBM Plex Mono',monospace !important;
        font-weight:700 !important; letter-spacing:.06em !important;
        border-radius:4px !important;
    }
    .stButton > button:hover {
        background:#1c2030 !important;
        border-color:#3a4a68 !important;
        color:#c8d8e8 !important;
    }
    hr { border-color:#1a1a28 !important; }
    [data-testid="stTab"] button { font-family:'IBM Plex Mono',monospace !important; }
    </style>
    """, unsafe_allow_html=True)

    # ── Header ────────────────────────────────────────────────
    st.markdown("""
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:4px">
      <span style="font-size:36px">📐</span>
      <div>
        <h1 style="margin:0;font-size:26px;color:#e8e8f8;font-family:'IBM Plex Mono',monospace">
          BLUESTAR ZONE INDICATOR
        </h1>
        <p style="margin:0;color:#4a4a7a;font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.12em">
          PDH 🟢 · MIDNIGHT OPEN 🟡 · PDL 🔴 · PREMIUM / DISCOUNT ICT
        </p>
      </div>
    </div>
    <hr>
    """, unsafe_allow_html=True)

    # ── Secrets ───────────────────────────────────────────────
    missing = [k for k in ("OANDA_ACCESS_TOKEN", "OANDA_ACCOUNT_ID")
               if k not in st.secrets]
    if missing:
        st.error(f"🔑 **Secret(s) manquant(s) :** `{'`, `'.join(missing)}`")
        st.stop()

    ACCESS_TOKEN = st.secrets["OANDA_ACCESS_TOKEN"]
    client = oandapyV20.API(access_token=ACCESS_TOKEN, environment="practice")

    # ── Mode selector ─────────────────────────────────────────
    tab_global, tab_detail = st.tabs(["📊 VUE GLOBALE — tous les actifs",
                                       "🔍 ZOOM — actif sélectionné"])

    # ════════════════════════════════════════════════════════
    #  TAB 1 : VUE GLOBALE
    # ════════════════════════════════════════════════════════
    with tab_global:
        st.markdown("<br>", unsafe_allow_html=True)

        col_run, col_filter = st.columns([2, 2])
        with col_run:
            run_global = st.button("🚀  SCANNER TOUTES LES ZONES", use_container_width=True)
        with col_filter:
            zone_filter = st.multiselect(
                "Filtrer par zone",
                ["PREMIUM", "DISCOUNT", "EXT HIGH", "EXT LOW", "EQUILIBRE"],
                default=[],
                placeholder="Toutes les zones"
            )

        if not run_global:
            st.markdown("""
            <div style="text-align:center;padding:50px 0;color:#2a2a4a">
              <div style="font-size:42px">📐</div>
              <div style="font-family:'IBM Plex Mono',monospace;font-size:13px;
                          letter-spacing:.1em;margin-top:10px;color:#2a2a5a">
                APPUIE SUR SCANNER POUR CHARGER TOUTES LES ZONES
              </div>
              <div style="font-size:11px;color:#1e1e3e;margin-top:8px;
                          font-family:'IBM Plex Mono',monospace;letter-spacing:.06em">
                PDH = LIGNE VERTE · MO = LIGNE JAUNE · PDL = LIGNE ROUGE
              </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            progress = st.progress(0)
            status   = st.empty()
            status.caption("⏳ Chargement des données…")

            all_data = fetch_all_levels(
                client, ASSETS,
                progress_cb=lambda p: progress.progress(p)
            )
            progress.empty()
            status.empty()

            # Filtrage
            filtered = all_data
            if zone_filter:
                filtered = [
                    d for d in all_data
                    if not d.get("error") and d["levels"]["zone"] in zone_filter
                ]

            if not filtered:
                st.info("Aucun actif correspondant aux filtres.")
            else:
                html_table = render_summary_table(
                    [d for d in all_data
                     if not zone_filter or
                     (not d.get("error") and d["levels"]["zone"] in zone_filter)]
                )
                st.markdown(html_table, unsafe_allow_html=True)

            st.markdown(f"""
            <div style="color:#2a2a4a;font-family:'IBM Plex Mono',monospace;
                        font-size:10px;text-align:right;margin-top:6px;letter-spacing:.06em">
              {len(all_data)} actifs · {datetime.now().strftime('%H:%M:%S')}
              · <span style="color:#FFE000">🌙 MO = 00:00 America/New_York</span>
            </div>
            """, unsafe_allow_html=True)

    # ════════════════════════════════════════════════════════
    #  TAB 2 : ZOOM ACTIF
    # ════════════════════════════════════════════════════════
    with tab_detail:
        st.markdown("<br>", unsafe_allow_html=True)

        col_sel, col_run2 = st.columns([3, 1])
        with col_sel:
            selected = st.selectbox("Choisir un actif", ASSETS, index=0)
        with col_run2:
            st.markdown("<div style='margin-top:24px'>", unsafe_allow_html=True)
            run_detail = st.button("🔍  ANALYSER", use_container_width=True)
            st.markdown("</div>", unsafe_allow_html=True)

        if not run_detail:
            st.markdown("""
            <div style="text-align:center;padding:40px 0;color:#2a2a4a">
              <div style="font-size:36px">🔍</div>
              <div style="font-family:'IBM Plex Mono',monospace;font-size:13px;
                          letter-spacing:.1em;margin-top:10px;color:#2a2a5a">
                SÉLECTIONNE UN ACTIF ET CLIQUE ANALYSER
              </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            with st.spinner(f"Chargement {selected}…"):
                df_d,   _ = fetch_oanda(client, selected, "D",   50)
                df_m15, _ = fetch_oanda(client, selected, "M15", 200)

            if df_d.empty or df_m15.empty:
                st.error("Données indisponibles pour cet actif.")
            else:
                lv = compute_levels(df_d, df_m15)
                if lv is None:
                    st.error("Impossible de calculer les niveaux.")
                else:
                    # ── Métriques ──────────────────────────────────
                    mc = st.columns(5)
                    ZONE_EMOJI = {
                        "PREMIUM": "▲", "DISCOUNT": "▼",
                        "EXT HIGH": "⚡", "EXT LOW": "⚡", "EQUILIBRE": "—"
                    }
                    ZONE_COL = {
                        "PREMIUM": "#c8952a", "DISCOUNT": "#4a98c8",
                        "EXT HIGH": "#e05a3a", "EXT LOW": "#3a9ece",
                        "EQUILIBRE": "#707070"
                    }

                    def fmt(v, ticker=""):
                        if v is None: return "—"
                        if v > 1000:  return f"{v:,.2f}"
                        elif v > 10:  return f"{v:.3f}"
                        else:         return f"{v:.5f}"

                    total_r = lv['pdh'] - lv['pdl']
                    if lv['midnight_open']:
                        pct_in_range = (lv['price'] - lv['pdl']) / total_r * 100 if total_r > 0 else 50
                    else:
                        pct_in_range = None

                    with mc[0]:
                        st.markdown(f"""
                        <div style="background:#121218;border:1px solid #1e1e2e;
                                    border-top:2px solid #39FF14;
                                    border-radius:6px;padding:12px 16px">
                          <div style="color:#404060;font-size:10px;letter-spacing:.1em;
                                      font-family:'IBM Plex Mono',monospace;margin-bottom:4px">
                            PDH 🟢
                          </div>
                          <div style="color:#39FF14;font-size:18px;font-weight:700;
                                      font-family:'IBM Plex Mono',monospace">
                            {fmt(lv['pdh'])}
                          </div>
                        </div>
                        """, unsafe_allow_html=True)

                    with mc[1]:
                        mo_label = "MO 🟡" + (" ⚠ fallback" if lv['mo_fallback'] else "")
                        st.markdown(f"""
                        <div style="background:#121218;border:1px solid #1e1e2e;
                                    border-top:2px solid #FFE000;
                                    border-radius:6px;padding:12px 16px">
                          <div style="color:#404060;font-size:10px;letter-spacing:.1em;
                                      font-family:'IBM Plex Mono',monospace;margin-bottom:4px">
                            {mo_label}
                          </div>
                          <div style="color:#FFE000;font-size:18px;font-weight:700;
                                      font-family:'IBM Plex Mono',monospace">
                            {fmt(lv['midnight_open'])}
                          </div>
                        </div>
                        """, unsafe_allow_html=True)

                    with mc[2]:
                        st.markdown(f"""
                        <div style="background:#121218;border:1px solid #1e1e2e;
                                    border-top:2px solid #FF003C;
                                    border-radius:6px;padding:12px 16px">
                          <div style="color:#404060;font-size:10px;letter-spacing:.1em;
                                      font-family:'IBM Plex Mono',monospace;margin-bottom:4px">
                            PDL 🔴
                          </div>
                          <div style="color:#FF003C;font-size:18px;font-weight:700;
                                      font-family:'IBM Plex Mono',monospace">
                            {fmt(lv['pdl'])}
                          </div>
                        </div>
                        """, unsafe_allow_html=True)

                    with mc[3]:
                        zc = ZONE_COL.get(lv['zone'], '#707070')
                        st.markdown(f"""
                        <div style="background:#121218;border:1px solid #1e1e2e;
                                    border-top:2px solid {zc};
                                    border-radius:6px;padding:12px 16px">
                          <div style="color:#404060;font-size:10px;letter-spacing:.1em;
                                      font-family:'IBM Plex Mono',monospace;margin-bottom:4px">
                            Prix actuel
                          </div>
                          <div style="color:{zc};font-size:18px;font-weight:700;
                                      font-family:'IBM Plex Mono',monospace">
                            {fmt(lv['price'])}
                          </div>
                        </div>
                        """, unsafe_allow_html=True)

                    with mc[4]:
                        zc = ZONE_COL.get(lv['zone'], '#707070')
                        st.markdown(f"""
                        <div style="background:#121218;border:1px solid {zc}44;
                                    border-top:2px solid {zc};
                                    border-radius:6px;padding:12px 16px">
                          <div style="color:#404060;font-size:10px;letter-spacing:.1em;
                                      font-family:'IBM Plex Mono',monospace;margin-bottom:4px">
                            Zone
                          </div>
                          <div style="color:{zc};font-size:18px;font-weight:700;
                                      font-family:'IBM Plex Mono',monospace">
                            {ZONE_EMOJI.get(lv['zone'], '—')} {lv['zone']}
                          </div>
                        </div>
                        """, unsafe_allow_html=True)

                    st.markdown("<br>", unsafe_allow_html=True)

                    # ── Barre visuelle + Explication ───────────────
                    col_bar, col_explain = st.columns([1, 2])

                    with col_bar:
                        bar_html = render_zone_bar(lv, selected)
                        st.markdown(bar_html, unsafe_allow_html=True)

                    with col_explain:
                        # Explication de la zone
                        zone = lv['zone']
                        mo_str = fmt(lv['midnight_open'])
                        pdh_str = fmt(lv['pdh'])
                        pdl_str = fmt(lv['pdl'])
                        price_str = fmt(lv['price'])

                        zone_explanations = {
                            "PREMIUM": {
                                "color": "#c8952a",
                                "title": "▲ ZONE PREMIUM",
                                "desc": f"Le prix ({price_str}) est <strong>au-dessus du Midnight Open</strong> ({mo_str}) et sous le PDH ({pdh_str}).<br><br>📌 Zone de <strong>vente institutionnelle</strong> — Les smart money distribuent dans cette zone. Cherche des setups SHORT avec confirmation (FVG, OB baissier).",
                                "bias": "BEARISH",
                                "bias_color": "#9e4a3a",
                            },
                            "DISCOUNT": {
                                "color": "#4a98c8",
                                "title": "▼ ZONE DISCOUNT",
                                "desc": f"Le prix ({price_str}) est <strong>en-dessous du Midnight Open</strong> ({mo_str}) et au-dessus du PDL ({pdl_str}).<br><br>📌 Zone d'<strong>accumulation institutionnelle</strong> — Les smart money achètent dans cette zone. Cherche des setups LONG avec confirmation (FVG, OB haussier).",
                                "bias": "BULLISH",
                                "bias_color": "#4d9467",
                            },
                            "EXT HIGH": {
                                "color": "#e05a3a",
                                "title": "⚡ EXTENSION HAUTE",
                                "desc": f"Le prix ({price_str}) est <strong>au-dessus du PDH</strong> ({pdh_str}).<br><br>⚠️ Hors range quotidien — Possible <strong>liquidity grab</strong> sur les stops au-dessus du PDH. Prudence : attendre le retour dans le range avant de trader.",
                                "bias": "ATTENTION",
                                "bias_color": "#e05a3a",
                            },
                            "EXT LOW": {
                                "color": "#3a9ece",
                                "title": "⚡ EXTENSION BASSE",
                                "desc": f"Le prix ({price_str}) est <strong>en-dessous du PDL</strong> ({pdl_str}).<br><br>⚠️ Hors range quotidien — Possible <strong>liquidity sweep</strong> sous le PDL. Attendre le retour dans le range ou une confirmation de continuation.",
                                "bias": "ATTENTION",
                                "bias_color": "#3a9ece",
                            },
                            "EQUILIBRE": {
                                "color": "#909090",
                                "title": "— EQUILIBRE",
                                "desc": f"Le prix ({price_str}) est au niveau du Midnight Open ({mo_str}).<br><br>Zone neutre — pas de biais directionnel clair. Attendre un éclatement clair du MO pour qualifier la zone.",
                                "bias": "NEUTRAL",
                                "bias_color": "#606060",
                            },
                        }
                        ze = zone_explanations.get(zone, zone_explanations["EQUILIBRE"])

                        # ADR journalier
                        total_range_pts = lv['pdh'] - lv['pdl']
                        if lv['midnight_open']:
                            pct_pos = (lv['price'] - lv['pdl']) / total_range_pts * 100
                            mo_pct  = (lv['midnight_open'] - lv['pdl']) / total_range_pts * 100
                        else:
                            pct_pos = 50.0
                            mo_pct  = 50.0

                        st.markdown(f"""
<div style="
  background:#0c0c14;border:1px solid #1a1a2e;
  border-left:3px solid {ze['color']};
  border-radius:6px;padding:18px 20px;
  font-family:'IBM Plex Mono',monospace;
">
  <div style="font-size:14px;font-weight:700;color:{ze['color']};
              letter-spacing:.1em;margin-bottom:10px">
    {ze['title']}
  </div>
  <div style="font-size:13px;color:#7a7a9a;line-height:1.8;margin-bottom:16px">
    {ze['desc']}
  </div>

  <!-- Range journalier horizontal -->
  <div style="margin-bottom:14px">
    <div style="font-size:9px;color:#303050;letter-spacing:.12em;
                text-transform:uppercase;margin-bottom:5px">
      Position dans le range journalier
    </div>
    <div style="position:relative;height:18px;background:#0a0a12;
                border-radius:3px;overflow:visible;">

      <!-- Zone DISCOUNT -->
      <div style="
        position:absolute;left:0;top:0;bottom:0;
        width:{mo_pct:.1f}%;
        background:rgba(74,120,152,0.2);border-radius:3px 0 0 3px;
      "></div>
      <!-- Zone PREMIUM -->
      <div style="
        position:absolute;top:0;bottom:0;
        left:{mo_pct:.1f}%;right:0;
        background:rgba(138,96,40,0.2);border-radius:0 3px 3px 0;
      "></div>

      <!-- MO marker -->
      <div style="
        position:absolute;top:-2px;bottom:-2px;
        left:{mo_pct:.1f}%;
        width:2px;background:#FFE000;
        box-shadow:0 0 6px #FFE00088;
      "></div>

      <!-- Price marker -->
      <div style="
        position:absolute;top:-4px;
        left:calc({pct_pos:.1f}% - 5px);
        width:10px;height:26px;
      ">
        <div style="
          width:0;height:0;
          border-left:5px solid transparent;
          border-right:5px solid transparent;
          border-top:8px solid {ze['color']};
          filter:drop-shadow(0 0 4px {ze['color']});
          margin-top:4px;
        "></div>
      </div>
    </div>
    <div style="
      display:flex;justify-content:space-between;
      font-size:9px;color:#303050;margin-top:3px;
    ">
      <span style="color:#FF003C">PDL {fmt(lv['pdl'])}</span>
      <span style="color:#FFE000">MO {fmt(lv['midnight_open'])}</span>
      <span style="color:#39FF14">PDH {fmt(lv['pdh'])}</span>
    </div>
  </div>

  <!-- Biais -->
  <div style="
    display:inline-block;
    padding:4px 12px;border-radius:3px;
    background:{ze['bias_color']}18;
    color:{ze['bias_color']};
    font-weight:700;font-size:11px;letter-spacing:.12em;
  ">BIAIS → {ze['bias']}</div>
</div>
                        """, unsafe_allow_html=True)

                    # ── Footer timestamp ───────────────────────────
                    st.markdown(f"""
                    <div style="color:#2a2a4a;font-family:'IBM Plex Mono',monospace;
                                font-size:10px;text-align:right;margin-top:8px;letter-spacing:.06em">
                      {selected} · {datetime.now().strftime('%H:%M:%S')}
                      · <span style="color:#FFE000">MO = 00:00 America/New_York</span>
                    </div>
                    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
