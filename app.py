# ==========================================
# BLUESTAR SCANNER V6.4 - ARCHITECTURE PYRAMIDE (CORRIGÉ)
# Critères en couches : Essentiels → Bonus
# ==========================================

import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

# ==========================================
# IMPORTS MANQUANTS (à adapter selon votre projet)
# ==========================================
# from your_api_module import OandaClient
# from your_engine_module import QuantEngine
# from your_config_module import ASSETS, get_asset_params, get_currency_strength_rsi


class SignalQuality:
    """
    Système de scoring en pyramide
    - Tier 1 (CORE) : 70% du score = Critères essentiels
    - Tier 2 (PREMIUM) : 20% du score = Zones + MTF
    - Tier 3 (ELITE) : 10% du score = Confluences avancées
    """
    
    @staticmethod
    def calculate_tier1_core(df_m5, df_h1, direction, live_price, midnight_open, pdl, pdh):
        """
        TIER 1 - CRITÈRES ESSENTIELS (70 points max)
        Ce sont vos critères TradingView de base
        """
        score = 0
        reasons = []
        
        # Protection contre les DataFrames vides
        if df_m5.empty or df_h1.empty or len(df_m5) < 4 or len(df_h1) < 4:
            return 0, ["❌ Données insuffisantes"]
        
        price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
        
        # 1. HMA H1 Direction (15 points)
        try:
            hma_h1 = QuantEngine.calculate_hma(df_h1['close'])
            if len(hma_h1) < 4:
                return 0, ["❌ HMA H1 - Données insuffisantes"]
            
            hma_h1_green = hma_h1.iloc[-2] > hma_h1.iloc[-3]
            
            if direction == "BUY":
                if hma_h1_green:
                    score += 15
                    reasons.append("✅ HMA H1 Verte")
                else:
                    return 0, ["❌ HMA H1 Rouge - REJET IMMÉDIAT"]
            else:
                if not hma_h1_green:
                    score += 15
                    reasons.append("✅ HMA H1 Rouge")
                else:
                    return 0, ["❌ HMA H1 Verte - REJET IMMÉDIAT"]
        except Exception as e:
            return 0, [f"❌ Erreur HMA H1: {str(e)}"]
        
        # 2. ADX H1 > 20 (15 points)
        try:
            adx_h1, adx_dir = QuantEngine.calculate_adx_and_di(df_h1)
            if adx_h1 >= 20:
                score += 15
                reasons.append(f"✅ ADX {adx_h1:.1f}")
            else:
                return 0, [f"❌ ADX {adx_h1:.1f} < 20 - REJET"]
            
            # Vérif direction ADX correspond
            if direction == "BUY" and adx_dir != 1:
                return 0, ["❌ ADX Direction Baissière"]
            if direction == "SELL" and adx_dir != -1:
                return 0, ["❌ ADX Direction Haussière"]
        except Exception as e:
            return 0, [f"❌ Erreur ADX: {str(e)}"]
        
        # 3. Heiken Ashi M5 Flip (20 points)
        try:
            ha_o_m5, ha_c_m5 = QuantEngine.get_ha_ohlc(df_m5)
            if len(ha_c_m5) < 4 or len(ha_o_m5) < 4:
                return 0, ["❌ HA M5 - Données insuffisantes"]
            
            if direction == "BUY":
                ha_prev_red = ha_c_m5.iloc[-3] < ha_o_m5.iloc[-3]
                ha_curr_green = ha_c_m5.iloc[-2] > ha_o_m5.iloc[-2]
                ha_flip = ha_prev_red and ha_curr_green
            else:
                ha_prev_green = ha_c_m5.iloc[-3] > ha_o_m5.iloc[-3]
                ha_curr_red = ha_c_m5.iloc[-2] < ha_o_m5.iloc[-2]
                ha_flip = ha_prev_green and ha_curr_red
            
            if ha_flip:
                score += 20
                reasons.append("✅ HA Flip Confirmé")
            else:
                return 0, ["❌ Pas de HA Flip - REJET"]
        except Exception as e:
            return 0, [f"❌ Erreur HA: {str(e)}"]
        
        # 4. Midnight Open Position (10 points)
        if midnight_open:
            try:
                if direction == "BUY" and price < midnight_open:
                    score += 10
                    reasons.append(f"✅ Prix sous Midnight ({price:.5f} < {midnight_open:.5f})")
                elif direction == "SELL" and price > midnight_open:
                    score += 10
                    reasons.append(f"✅ Prix sur Midnight ({price:.5f} > {midnight_open:.5f})")
                else:
                    score += 3  # Pénalité légère au lieu de rejet
                    reasons.append(f"⚠️ Prix mauvais côté Midnight")
            except Exception as e:
                reasons.append(f"⚠️ Erreur Midnight: {str(e)}")
        
        # 5. Z-Score Extrême (10 points)
        try:
            z_curr, z_prev = QuantEngine.get_zscore_status(df_m5, lookback=20)
            
            if direction == "BUY":
                if z_curr < -1.5 and z_curr > z_prev:
                    score += 10
                    reasons.append(f"✅ Z-Score {z_curr:.2f} (Oversold Bounce)")
                elif z_curr < -1.0:
                    score += 5
                    reasons.append(f"⚠️ Z-Score {z_curr:.2f} (Faible)")
            else:
                if z_curr > 1.5 and z_curr < z_prev:
                    score += 10
                    reasons.append(f"✅ Z-Score {z_curr:.2f} (Overbought Drop)")
                elif z_curr > 1.0:
                    score += 5
                    reasons.append(f"⚠️ Z-Score {z_curr:.2f} (Faible)")
        except Exception as e:
            reasons.append(f"⚠️ Erreur Z-Score: {str(e)}")
        
        return score, reasons
    
    @staticmethod
    def calculate_tier2_premium(df_m5, df_m15, df_d, df_w, symbol, direction, atr, cs_scores):
        """
        TIER 2 - CRITÈRES PREMIUM (20 points max)
        OB/FVG, MTF Alignment, Currency Strength
        """
        score = 0
        reasons = []
        
        try:
            # 1. Order Block OU FVG OU PDL (10 points max)
            pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
            price = df_m5['close'].iloc[-1]
            
            ob_valid, ob_zone = QuantEngine.detect_valid_ob(df_m5, atr, direction)
            fvg_valid, fvg_zone = QuantEngine.detect_fvg(df_m5, atr, direction)
            
            if ob_valid:
                score += 10
                reasons.append(f"✅ ORDER BLOCK Actif ({ob_zone[0]:.5f}-{ob_zone[1]:.5f})")
            elif fvg_valid:
                score += 8
                reasons.append(f"✅ FVG Actif ({fvg_zone[0]:.5f}-{fvg_zone[1]:.5f})")
            elif pdl and direction == "BUY" and abs(price - pdl) < atr * 0.5:
                score += 6
                reasons.append(f"✅ Proche PDL ({pdl:.5f})")
            elif pdh and direction == "SELL" and abs(price - pdh) < atr * 0.5:
                score += 6
                reasons.append(f"✅ Proche PDH ({pdh:.5f})")
            else:
                reasons.append("⚠️ Pas de zone optimale (mais OK)")
        except Exception as e:
            reasons.append(f"⚠️ Erreur zones: {str(e)}")
        
        # 2. MTF Alignment D/W (5 points)
        try:
            grade = QuantEngine.get_institutional_grade_v2(df_d, df_w, direction)
            if grade == "A+":
                score += 5
                reasons.append("✅ MTF A+ (D+W aligned)")
            elif grade == "A":
                score += 3
                reasons.append("⚠️ MTF A (D only)")
        except Exception as e:
            reasons.append(f"⚠️ Erreur MTF: {str(e)}")
        
        # 3. Currency Strength (5 points)
        try:
            if "_" in symbol and cs_scores:
                base, quote = symbol.split('_')
                gap = cs_scores.get(base, 0) - cs_scores.get(quote, 0)
                if direction == "BUY" and gap > 0.5:
                    score += 5
                    reasons.append(f"✅ CS Aligné ({base} > {quote})")
                elif direction == "SELL" and gap < -0.5:
                    score += 5
                    reasons.append(f"✅ CS Aligné ({quote} > {base})")
                else:
                    reasons.append(f"⚠️ CS Neutre (Δ={gap:.2f})")
        except Exception as e:
            reasons.append(f"⚠️ Erreur CS: {str(e)}")
        
        return score, reasons
    
    @staticmethod
    def calculate_tier3_elite(df_m15, direction):
        """
        TIER 3 - CONFLUENCES ÉLITES (10 points max)
        Structure M15, Momentum
        """
        score = 0
        reasons = []
        
        try:
            # Protection contre DataFrames vides
            if df_m15.empty or len(df_m15) < 5:
                return score, reasons
            
            # 1. Structure M15 (5 points)
            if direction == "BUY":
                structure_ok = (df_m15['low'].iloc[-2] > df_m15['low'].iloc[-4])
            else:
                structure_ok = (df_m15['high'].iloc[-2] < df_m15['high'].iloc[-4])
            
            if structure_ok:
                score += 5
                reasons.append("✅ Structure M15 Robuste")
            
            # 2. HMA M15 Aligned (5 points)
            hma_m15 = QuantEngine.calculate_hma(df_m15['close'])
            if len(hma_m15) >= 3:
                hma_m15_green = hma_m15.iloc[-2] > hma_m15.iloc[-3]
                
                if direction == "BUY" and hma_m15_green:
                    score += 5
                    reasons.append("✅ HMA M15 Verte")
                elif direction == "SELL" and not hma_m15_green:
                    score += 5
                    reasons.append("✅ HMA M15 Rouge")
        except Exception as e:
            reasons.append(f"⚠️ Erreur Elite: {str(e)}")
        
        return score, reasons


# ==========================================
# FONCTION PRINCIPALE V6.4 - UTILISE LA PYRAMIDE
# ==========================================

def calculate_signal_probability_v640(
    df_m5, df_m15, df_h1, df_d, df_w,
    symbol, direction, live_price, spread, cs_scores
):
    """
    Scoring Pyramide:
    - Tier 1 (Core): 70 points → Si < 60, REJET
    - Tier 2 (Premium): 20 points → Bonus
    - Tier 3 (Elite): 10 points → Bonus
    Total: 100 points max
    
    Seuils de qualité:
    - 85-100: ELITE (🏆)
    - 75-84: PREMIUM (⭐)
    - 60-74: STANDARD (✅)
    - <60: REJET (❌)
    """
    
    try:
        price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
        atr = QuantEngine.calculate_atr(df_m5)
        midnight_open = QuantEngine.get_midnight_open_ny(df_m5)
        pdh, pdl = QuantEngine.get_pdh_pdl(df_d)
        
        # TIER 1 - CRITÈRES ESSENTIELS
        tier1_score, tier1_reasons = SignalQuality.calculate_tier1_core(
            df_m5, df_h1, direction, live_price, midnight_open, pdl, pdh
        )
        
        # Si Tier 1 échoue (score 0), REJET IMMÉDIAT
        if tier1_score == 0:
            return 0, {}, 0, tier1_reasons[0], {}
        
        # Si Tier 1 < 60, signal trop faible
        if tier1_score < 60:
            return 0, {}, 0, f"Score Core insuffisant ({tier1_score}/70)", {}
        
        # TIER 2 - CRITÈRES PREMIUM (optionnel mais bonifie)
        tier2_score, tier2_reasons = SignalQuality.calculate_tier2_premium(
            df_m5, df_m15, df_d, df_w, symbol, direction, atr, cs_scores
        )
        
        # TIER 3 - CRITÈRES ÉLITES (optionnel)
        tier3_score, tier3_reasons = SignalQuality.calculate_tier3_elite(
            df_m15, direction
        )
        
        # Score final sur 100
        total_score = tier1_score + tier2_score + tier3_score
        
        # Classification
        if total_score >= 85:
            quality = "ELITE 🏆"
        elif total_score >= 75:
            quality = "PREMIUM ⭐"
        elif total_score >= 60:
            quality = "STANDARD ✅"
        else:
            return 0, {}, 0, f"Score total insuffisant ({total_score}/100)", {}
        
        # Calcul SL/TP
        params = get_asset_params(symbol)
        sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
        tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
        
        # Assemblage détails
        all_reasons = tier1_reasons + tier2_reasons + tier3_reasons
        
        # Session calculée de manière sûre
        try:
            current_session = QuantEngine.get_trading_session(datetime.now(pytz.utc))
        except:
            current_session = "N/A"
        
        details = {
            "quality": quality,
            "score_breakdown": f"Core:{tier1_score} + Premium:{tier2_score} + Elite:{tier3_score} = {total_score}/100",
            "reasons": all_reasons,
            "midnight": f"{midnight_open:.5f}" if midnight_open else "N/A",
            "pdh_pdl": f"{pdh:.5f} / {pdl:.5f}" if pdh and pdl else "N/A",
            "session": current_session,
        }
        
        # Probabilité normalisée (0-1)
        probability = total_score / 100
        
        return probability, details, atr / price * 100, None, {}
    
    except Exception as e:
        return 0, {}, 0, f"Erreur calcul: {str(e)}", {}


# ==========================================
# AFFICHAGE V6.4 - AVEC BADGES QUALITÉ
# ==========================================

def display_sig_v640(s):
    """Affiche un signal avec son badge qualité"""
    try:
        is_buy = s['type'] == 'BUY'
        col_type = "#10b981" if is_buy else "#ef4444"
        bg = "linear-gradient(90deg, #064e3b 0%, #065f46 100%)" if is_buy else "linear-gradient(90deg, #7f1d1d 0%, #991b1b 100%)"
        d = s['details']
        
        # Badge qualité
        quality_badge = ""
        if "ELITE" in d['quality']:
            quality_badge = "<span class='badge' style='background:#FFD700;color:black;font-size:1em;padding:4px 12px;border-radius:4px;'>🏆 ELITE</span>"
        elif "PREMIUM" in d['quality']:
            quality_badge = "<span class='badge' style='background:#C0C0C0;color:black;font-size:1em;padding:4px 12px;border-radius:4px;'>⭐ PREMIUM</span>"
        else:
            quality_badge = "<span class='badge' style='background:#3b82f6;color:white;font-size:1em;padding:4px 12px;border-radius:4px;'>✅ STANDARD</span>"
        
        with st.expander(f"{'📈' if is_buy else '📉'} {s['symbol']}  |  {s['type']}  |  {s['score_display']:.0f}/100  {d['quality']}", expanded=True):
            st.markdown(f"""
            <div style="background:{bg};padding:15px;border-radius:8px;border:2px solid {col_type};margin-bottom:10px;">
                <span style="font-size:1.5em;font-weight:900;color:white;">{s['symbol']}</span>
                <span style="float:right;color:white;font-size:1.2em;">{s['price']:.5f}</span><br>
                <div style='margin-top:10px;'>{quality_badge}</div>
            </div>""", unsafe_allow_html=True)
            
            st.info(f"**Score Breakdown:** {d['score_breakdown']}")
            
            # Afficher toutes les raisons
            st.markdown("### 📋 Critères Validés")
            for reason in d['reasons']:
                if "✅" in reason:
                    st.success(reason)
                elif "⚠️" in reason:
                    st.warning(reason)
            
            c1, c2 = st.columns(2)
            c1.metric("Midnight", d.get('midnight', 'N/A'))
            c2.metric("PDH / PDL", d.get('pdh_pdl', 'N/A'))
            
            col_sl, col_tp = st.columns(2)
            col_sl.info(f"🛑 SL: {s['sl']:.5f}")
            col_tp.success(f"🎯 TP: {s['tp']:.5f}")
    
    except Exception as e:
        st.error(f"Erreur affichage signal: {str(e)}")


# ==========================================
# SCANNER V6.4 - SIMPLIFIÉ
# ==========================================

def run_scan_v640(api, min_score, current_time_utc, filter_asian):
    """
    Scanner avec scoring pyramide
    min_score: 60-100 (recommandé: 75 pour qualité)
    """
    cs_scores = get_currency_strength_rsi(api)
    signals = []
    rejected_log = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, sym in enumerate(ASSETS):
        progress_bar.progress((i+1)/len(ASSETS))
        status_text.markdown(f"⏳ Scan: **{sym}** ({i+1}/{len(ASSETS)})")
        
        try:
            # Filtrage session Asiatique
            if filter_asian:
                try:
                    session = QuantEngine.get_trading_session(current_time_utc)
                    if session == "ASIAN":
                        if "XAU" not in sym and "US30" not in sym:
                            rejected_log.append(f"{sym}: Session Asiatique")
                            continue
                except:
                    pass  # Continue si erreur de session
            
            # Lazy loading H1 d'abord
            df_h1 = api.get_candles(sym, "H1", 50)
            if df_h1.empty: 
                rejected_log.append(f"{sym}: Pas de données H1")
                continue
            
            # Pré-check ADX
            adx_h1, adx_dir = QuantEngine.calculate_adx_and_di(df_h1)
            if adx_h1 < 20:
                rejected_log.append(f"{sym}: ADX {adx_h1:.1f} < 20")
                continue
            
            # Chargement complet
            df_m15 = api.get_candles(sym, "M15", 100)
            df_m5 = api.get_candles(sym, "M5", 200)
            df_d = api.get_candles(sym, "D", 250)
            df_w = api.get_candles(sym, "W", 150)
            
            live_price, spread_pips = api.get_realtime_price_and_spread(sym)
            
            if df_m5.empty or df_m15.empty or df_d.empty or df_w.empty: 
                rejected_log.append(f"{sym}: Données incomplètes")
                continue
            
            # Test BUY et SELL
            for direction in ["BUY", "SELL"]:
                # Vérif direction ADX correspond
                if direction == "BUY" and adx_dir != 1: continue
                if direction == "SELL" and adx_dir != -1: continue
                
                prob, details, atr_pct, reject_reason, _ = calculate_signal_probability_v640(
                    df_m5, df_m15, df_h1, df_d, df_w, sym, direction, 
                    live_price, spread_pips, cs_scores
                )
                
                if reject_reason:
                    rejected_log.append(f"{sym} {direction}: {reject_reason}")
                    continue
                
                # Filtrage par score minimum
                score_100 = prob * 100
                if score_100 < min_score:
                    rejected_log.append(f"{sym} {direction}: Score {score_100:.0f} < {min_score}")
                    continue
                
                # Calcul SL/TP
                price = live_price if live_price > 0 else df_m5['close'].iloc[-1]
                atr = QuantEngine.calculate_atr(df_m5)
                params = get_asset_params(sym)
                sl = price - (atr * params['sl_base']) if direction == "BUY" else price + (atr * params['sl_base'])
                tp = price + (atr * params['tp_rr']) if direction == "BUY" else price - (atr * params['tp_rr'])
                
                signals.append({
                    'symbol': sym,
                    'type': direction,
                    'price': price,
                    'prob': prob,
                    'score_display': score_100,
                    'details': details,
                    'atr_pct': atr_pct,
                    'sl': sl,
                    'tp': tp,
                    'rr': params['tp_rr'],
                    'spread': spread_pips
                })
        
        except Exception as e:
            rejected_log.append(f"❌ {sym}: {str(e)[:60]}")
            continue
    
    progress_bar.empty()
    status_text.empty()
    return sorted(signals, key=lambda x: x['prob'], reverse=True), rejected_log


# ==========================================
# MAIN V6.4
# ==========================================

def main():
    st.set_page_config(page_title="BLUESTAR V6.4", page_icon="🛡️", layout="wide")
    
    st.title("🛡️ BLUESTAR V6.4 - Architecture Pyramide")
    st.markdown("<p style='text-align:center;color:#94a3b8;'>Core Criteria (70%) + Premium Zones (20%) + Elite Confluences (10%)</p>", unsafe_allow_html=True)
    
    current_time_utc = datetime.now(pytz.utc)
    
    # Calcul de session de manière sûre
    try:
        session = QuantEngine.get_trading_session(current_time_utc)
    except Exception as e:
        session = "N/A"
        st.warning(f"⚠️ Impossible de déterminer la session: {str(e)}")
    
    with st.sidebar:
        st.header("⚙️ Paramètres V6.4")
        
        min_score = st.slider("Score Minimum", 60, 95, 75, 5,
            help="60-74: Standard | 75-84: Premium | 85+: Elite")
        
        filter_asian = st.checkbox("🕶️ Filtrer Session Asiatique", value=True)
        
        st.markdown("---")
        st.info(f"""
        **Session Actuelle:** {session}
        
        **Architecture Pyramide:**
        - ✅ **Core (70pts):** HMA H1, ADX>20, HA Flip, Midnight, Z-Score
        - ⭐ **Premium (20pts):** OB/FVG/PDL, MTF D/W, CS
        - 🏆 **Elite (10pts):** Structure M15, HMA M15
        
        **Les zones (OB/FVG) sont BONUS, pas obligatoires !**
        """)
    
    if st.button("🔍 SCANNER V6.4", type="primary"):
        with st.spinner("Analyse Pyramide en cours..."):
            try:
                api = OandaClient()
                results, logs = run_scan_v640(api, min_score, current_time_utc, filter_asian)
            except Exception as e:
                st.error(f"❌ Erreur lors de l'initialisation: {str(e)}")
                return
        
        if not results:
            st.warning("⚠️ Aucun signal validé.")
            with st.expander("📋 Logs de rejet (50 premiers)"):
                for log in logs[:50]: 
                    st.text(log)
        else:
            st.success(f"✅ {len(results)} Signal(s) - Score ≥ {min_score}")
            
            for r in results:
                display_sig_v640(r)
            
            with st.expander("📊 Statistiques de qualité"):
                elite = sum(1 for r in results if "ELITE" in r['details']['quality'])
                premium = sum(1 for r in results if "PREMIUM" in r['details']['quality'])
                standard = len(results) - elite - premium
                
                col1, col2, col3 = st.columns(3)
                col1.metric("🏆 Elite", elite)
                col2.metric("⭐ Premium", premium)
                col3.metric("✅ Standard", standard)
                
                avg_score = sum(r['score_display'] for r in results) / len(results)
                st.metric("Score Moyen", f"{avg_score:.1f}/100")

if __name__ == "__main__":
    main()
   
