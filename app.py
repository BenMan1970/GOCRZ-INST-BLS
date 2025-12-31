# ==========================================
# PROBABILITÉ (LOGIQUE CORRIGÉE V3.3)
# ==========================================
def calculate_signal_probability(df_m5, df_h4, df_d, df_w, symbol, direction):
    prob_factors = []
    weights = []
    details = {}
    
    params = get_asset_params(symbol)
    atr = QuantEngine.calculate_atr(df_m5)
    atr_pct = (atr / df_m5['close'].iloc[-1]) * 100
    
    if atr_pct < params['atr_threshold'] * 0.5:
        return 0, {}, atr_pct 
    
    # 1. VOLATILITY SCORE (Multiplicateur)
    vol_score = min(atr_pct / params['atr_threshold'], 2.0)
    details['vol_score'] = vol_score
    vol_conf = min(vol_score, 1.2) / 1.2 
    
    # 2. RSI MOMENTUM (20%)
    rsi_serie = QuantEngine.calculate_rsi(df_m5)
    rsi_val = rsi_serie.iloc[-1]
    rsi_mom = rsi_val - rsi_serie.iloc[-2]
    
    rsi_prob = 0
    if direction == "BUY":
        if rsi_serie.iloc[-2] < 50 and rsi_val >= 50: rsi_prob = 0.85
        elif rsi_val > 50 and rsi_mom > 0: rsi_prob = 0.60
    else:
        if rsi_serie.iloc[-2] > 50 and rsi_val <= 50: rsi_prob = 0.85
        elif rsi_val < 50 and rsi_mom < 0: rsi_prob = 0.60
            
    prob_factors.append(rsi_prob)
    weights.append(0.20)
    details['rsi_mom'] = abs(rsi_mom)
    
    # 3. STRUCTURE Z-SCORE (15%)
    z_score_struc = QuantEngine.detect_structure_zscore(df_h4, 20)
    struc_score = 0
    if direction == "BUY":
        if z_score_struc == 1: struc_score = 0.9
        elif z_score_struc == 0: struc_score = 0.5
    else:
        if z_score_struc == -1: struc_score = 0.9
        elif z_score_struc == 0: struc_score = 0.5
    prob_factors.append(struc_score)
    weights.append(0.15)
    details['structure_z'] = z_score_struc
    
    # 4. MTF BIAS (20%)
    mtf_bias = QuantEngine.get_mtf_bias(df_d, df_w)
    mtf_score = 0.5
    if direction == "BUY":
        if mtf_bias == "STRONG_BULL": mtf_score = 0.95
        elif mtf_bias == "BULL": mtf_score = 0.80
        elif mtf_bias == "NEUTRAL": mtf_score = 0.50
        else: mtf_score = 0.10
    else:
        if mtf_bias == "STRONG_BEAR": mtf_score = 0.95
        elif mtf_bias == "BEAR": mtf_score = 0.80
        elif mtf_bias == "NEUTRAL": mtf_score = 0.50
        else: mtf_score = 0.10
    prob_factors.append(mtf_score)
    weights.append(0.20)
    details['mtf_bias'] = mtf_bias
    
    # 5. MIDNIGHT LOGIC (25% - CRITIQUE)
    # C'est ici que la correction opère
    midnight_price = QuantEngine.get_midnight_open_ny(df_m5)
    midnight_score = 0.5 
    curr_price = df_m5['close'].iloc[-1]
    
    details['midnight_status'] = "UNKNOWN"
    is_location_bad = False # Flag pour pénalité
    
    if midnight_price:
        if direction == "BUY":
            if curr_price <= midnight_price: 
                midnight_score = 1.0 # BUY LOW (Perfect)
                details['midnight_status'] = "DISCOUNT (BUY ✅)"
            else:
                midnight_score = 0.0 # BUY HIGH (Bad)
                details['midnight_status'] = "PREMIUM (RISKY ⚠️)"
                is_location_bad = True
        else: # SELL
            if curr_price >= midnight_price:
                midnight_score = 1.0 # SELL HIGH (Perfect)
                details['midnight_status'] = "PREMIUM (SELL ✅)"
            else:
                midnight_score = 0.0 # SELL LOW (Bad)
                details['midnight_status'] = "DISCOUNT (RISKY ⚠️)"
                is_location_bad = True
    
    prob_factors.append(midnight_score)
    weights.append(0.25) # Poids augmenté

    # 6. FVG/ADX (20%)
    fvg_active, fvg_type = QuantEngine.detect_smart_fvg(df_m5, atr)
    details['fvg_align'] = fvg_active
    
    adx_val = QuantEngine.calculate_adx(df_h4)
    details['adx_val'] = adx_val
    
    # Filtre ADX Strict
    if adx_val < 18: return 0, details, atr_pct 
    
    extra_score = 0.5
    if adx_val > 22: extra_score += 0.2
    if fvg_active and ((direction=="BUY" and fvg_type=="BULL") or (direction=="SELL" and fvg_type=="BEAR")):
        extra_score += 0.3
    
    prob_factors.append(min(extra_score, 1.0))
    weights.append(0.20)

    # CALCUL FINAL
    total_weight = sum(weights)
    weighted_prob = sum(p * w for p, w in zip(prob_factors, weights)) / total_weight
    
    # Pénalité Fatale si Mauvaise Localisation (empêche les trades "Chasing")
    if is_location_bad:
        weighted_prob -= 0.15 # Retire 15% de probabilité brute
        
    final_score = max(0, weighted_prob * vol_conf)
    
    return final_score, details, atr_pct
