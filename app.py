# ===============================
# BLUESTAR SNIPER V10 ENGINE
# ===============================

class QuantEngine:

    # ===============================
    # ATR WILDER
    # ===============================
    @staticmethod
    def calculate_atr_wilder(df, period=14):

        high = df['high']
        low = df['low']
        close = df['close']

        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1/period, adjust=False).mean()

        return atr.iloc[-1]


    # ===============================
    # ADX WILDER
    # ===============================
    @staticmethod
    def adx_wilder(df, period=14):

        high = df['high']
        low = df['low']
        close = df['close']

        plus_dm = high.diff()
        minus_dm = low.diff().abs()

        plus_dm[plus_dm < minus_dm] = 0
        minus_dm[minus_dm < plus_dm] = 0

        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1/period, adjust=False).mean()

        plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / atr)

        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100

        adx = dx.ewm(alpha=1/period).mean()

        return adx.iloc[-1], plus_di.iloc[-1], minus_di.iloc[-1]


    # ===============================
    # HULL MOVING AVERAGE
    # ===============================
    @staticmethod
    def hma(series, period=55):

        half = int(period / 2)
        sqrt = int(np.sqrt(period))

        wma1 = series.rolling(half).mean()
        wma2 = series.rolling(period).mean()

        raw_hma = 2 * wma1 - wma2

        hma = raw_hma.rolling(sqrt).mean()

        return hma


# ===============================
# ICT PD ARRAYS
# ===============================

def pd_arrays_location(price, pdh, pdl):

    dealing_range = pdh - pdl

    equilibrium = pdl + dealing_range * 0.5
    discount_25 = pdl + dealing_range * 0.25
    premium_75 = pdl + dealing_range * 0.75

    if price < discount_25:
        return "DEEP_DISCOUNT"

    elif price < equilibrium:
        return "DISCOUNT"

    elif price < premium_75:
        return "PREMIUM"

    else:
        return "DEEP_PREMIUM"



# ===============================
# SIGNAL ENGINE V10
# ===============================

def calculate_signal_v10(df_m5, df_d):

    reasons = []
    score = 0

    price = df_m5['close'].iloc[-1]

    # ===============================
    # PD ARRAYS
    # ===============================

    pdh = df_d['high'].iloc[-2]
    pdl = df_d['low'].iloc[-2]

    location = pd_arrays_location(price, pdh, pdl)

    if location == "DEEP_DISCOUNT":
        score += 30
        reasons.append("💎 Deep Discount")

    elif location == "DISCOUNT":
        score += 20
        reasons.append("Discount Zone")

    elif location == "PREMIUM":
        score -= 10
        reasons.append("Premium Area")

    elif location == "DEEP_PREMIUM":
        score -= 30
        reasons.append("Deep Premium")


    # ===============================
    # TREND (HMA)
    # ===============================

    hma = QuantEngine.hma(df_m5['close'], 55)

    if price > hma.iloc[-1]:

        score += 10
        direction = "BUY"
        reasons.append("Trend Up (HMA)")

    else:

        score += 10
        direction = "SELL"
        reasons.append("Trend Down (HMA)")


    # ===============================
    # ATR
    # ===============================

    atr = QuantEngine.calculate_atr_wilder(df_m5)

    atr_mean = (df_m5['high'] - df_m5['low']).rolling(20).mean().iloc[-1]

    if atr > atr_mean:

        score += 10
        reasons.append("ATR Expansion")


    # ===============================
    # ADX
    # ===============================

    adx, plus_di, minus_di = QuantEngine.adx_wilder(df_m5)

    if adx > 25:

        score += 15
        reasons.append(f"Strong Trend ADX {adx:.1f}")

    elif adx > 20:

        score += 8
        reasons.append(f"Moderate Trend ADX {adx:.1f}")

    else:

        score -= 5
        reasons.append("Weak Trend")


    # ===============================
    # QUALITY CLASSIFICATION
    # ===============================

    if score >= 70:
        quality = "A+ SETUP"

    elif score >= 55:
        quality = "A SETUP"

    elif score >= 40:
        quality = "B SETUP"

    else:
        quality = "IGNORE"


    return {

        "direction": direction,
        "score": score,
        "quality": quality,
        "location": location,
        "adx": round(adx,2),
        "atr": round(atr,5),
        "reasons": reasons
    }
