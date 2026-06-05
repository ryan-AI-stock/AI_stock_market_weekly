"""Indicator calculation helpers for weekly stock reports."""

import pandas as pd


UP_COLOR = "#c0392b"
DOWN_COLOR = "#168f4d"
NEUTRAL_COLOR = "#95a5a6"


def calc_indicators(df: pd.DataFrame, scfg: dict) -> pd.DataFrame:
    ma = scfg["ma_periods"]
    thr = scfg["thresholds"]
    s, m, l = ma["short"], ma["mid"], ma["long"]

    df[f"MA{s}"] = df["Close"].rolling(s).mean()
    df[f"MA{m}"] = df["Close"].rolling(m).mean()
    df[f"MA{l}"] = df["Close"].rolling(l).mean()

    # BIAS60（季線乖離，固定60日，用於Z-Score）
    ma60 = df["Close"].rolling(60).mean()
    df["BIAS60"] = (df["Close"] - ma60) / ma60 * 100
    b60_clean = df["BIAS60"].dropna()
    p_low = thr.get("bias60_p_low", 5)
    p_high = thr.get("bias60_p_high", 95)
    df.attrs["bias60_p_high"] = float(b60_clean.quantile(p_high / 100))
    df.attrs["bias60_p_low"] = float(b60_clean.quantile(p_low / 100))
    df.attrs["bias60_mean"] = float(b60_clean.mean())
    df.attrs["bias60_std"] = float(b60_clean.std())
    df["BIAS60_Z"] = (df["BIAS60"] - df.attrs["bias60_mean"]) / df.attrs["bias60_std"]

    # 短線乖離率（依各股 mid MA）
    df["Bias20"] = (df["Close"] - df[f"MA{m}"]) / df[f"MA{m}"] * 100

    # KD
    low_min = df["Low"].rolling(9).min()
    high_max = df["High"].rolling(9).max()
    rsv = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["K"] = rsv.ewm(com=2, adjust=False).mean()
    df["D"] = df["K"].ewm(com=2, adjust=False).mean()

    # MACD
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["Signal"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["DIF"] - df["Signal"]

    # 量能趨勢
    vp = thr["vol_ma_period"]
    df["Vol_MA"] = df["Volume"].rolling(vp).mean()
    df["Vol_Trend"] = df["Vol_MA"] - df["Vol_MA"].shift(3)

    # OBV
    obv = [0]
    for i in range(1, len(df)):
        if df["Close"].iloc[i] > df["Close"].iloc[i - 1]:
            obv.append(obv[-1] + df["Volume"].iloc[i])
        elif df["Close"].iloc[i] < df["Close"].iloc[i - 1]:
            obv.append(obv[-1] - df["Volume"].iloc[i])
        else:
            obv.append(obv[-1])
    df["OBV"] = obv
    df["OBV_MA"] = df["OBV"].rolling(thr["obv_ma_period"]).mean()

    return df


def eval_bias60(df: pd.DataFrame, scfg: dict) -> dict:
    latest = df.iloc[-1]
    bias60 = float(latest["BIAS60"])
    z = float(latest["BIAS60_Z"])
    p_high = df.attrs["bias60_p_high"]
    p_low = df.attrs["bias60_p_low"]
    p_high_pct = scfg["thresholds"].get("bias60_p_high", 95)
    p_low_pct = scfg["thresholds"].get("bias60_p_low", 5)
    can_lock = scfg.get("bias60_locked", True)

    if bias60 >= p_high:
        zone = "overheated"
        locked = can_lock
        label = f"🔥 過熱{'鎖定' if can_lock else '警示'}（季線乖離{bias60:.1f}%，歷史{p_high_pct}%分位）"
        color = UP_COLOR
        note = f"Z={z:.2f}｜超過歷史{p_high_pct}%分位({p_high:.1f}%)｜{'正向條件暫停計入' if can_lock else '僅警示，不鎖定'}"
    elif bias60 <= p_low:
        zone = "oversold"
        locked = False
        label = f"❄️ 超跌觀察區（季線乖離{bias60:.1f}%，歷史{p_low_pct}%分位）"
        color = DOWN_COLOR
        note = f"Z={z:.2f}｜低於歷史{p_low_pct}%分位({p_low:.1f}%)｜統計超跌觀察區"
    else:
        zone = "normal"
        locked = False
        label = f"正常範圍（季線乖離{bias60:.1f}%）"
        color = NEUTRAL_COLOR
        note = f"Z={z:.2f}｜介於{p_low_pct}%({p_low:.1f}%)～{p_high_pct}%({p_high:.1f}%)分位之間"

    return dict(zone=zone, locked=locked, bias60=bias60,
                z_score=z, p_high=p_high, p_low=p_low,
                label=label, color=color, note=note)


def calc_pyramid(df: pd.DataFrame, scfg: dict, signal_level: str) -> dict:
    py = scfg.get("pyramid", {})
    drop_step = py.get("add_per_drop_pct", 5.0)
    time_days = py.get("time_rebalance_days", 20)

    close = float(df["Close"].iloc[-1])
    recent = df["Close"].iloc[-time_days:]
    high_ref = float(recent.max())
    drop_pct = (close - high_ref) / high_ref * 100
    range_pct = (float(recent.max()) - float(recent.min())) / float(recent.min()) * 100
    is_consolidating = range_pct < 5.0
    suggestions = []

    if signal_level.startswith("BUY_"):
        batches = int(abs(drop_pct) / drop_step) if drop_pct < 0 else 0
        if batches == 0:
            suggestions.append("📌 正向條件初步成立：進入第一層觀察")
        else:
            suggestions.append(
                f"📌 正向條件層級 {batches+1}：距高點回落 {abs(drop_pct):.1f}%")
            suggestions.append(
                f"　　累計已達 {batches} 層回落條件（每回落 {drop_step:.0f}% 增加一層觀察）")
        if is_consolidating:
            suggestions.append(
                f"⏱️ 時間條件提醒：近 {time_days} 日盤整幅度僅 {range_pct:.1f}%，"
                f"列入後續條件觀察")

    return dict(drop_pct=drop_pct, is_consolidating=is_consolidating,
                range_pct=range_pct, suggestions=suggestions)
