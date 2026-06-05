"""Data source helpers for the weekly stock market report."""

import requests
import yfinance as yf
import pandas as pd
from datetime import date, datetime, timedelta, timezone, time

TAIPEI_TZ = timezone(timedelta(hours=8))
WEEKLY_REPORT_START_TIME = time(15, 0)


def _parse_int(value) -> int:
    try:
        return int(str(value).replace(",", "").replace(" ", ""))
    except Exception:
        return 0


def _parse_float(value) -> float | None:
    try:
        raw = str(value).replace(",", "").replace(" ", "").strip()
        if raw in ("", "--", "-"):
            return None
        return float(raw)
    except Exception:
        return None


def _find_field(fields: list, *keywords: str) -> int | None:
    for idx, field in enumerate(fields):
        if all(keyword in field for keyword in keywords):
            return idx
    return None


def _find_exact_field(fields: list, name: str) -> int | None:
    try:
        return fields.index(name)
    except ValueError:
        return None


# ── 三大法人資料 ─────────────────────────────────────────────
def fetch_institutional(ticker: str, lookback_days: int = 7) -> dict:
    stock_id = ticker.upper().replace(".TW", "").replace(".TWO", "")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.twse.com.tw/",
    }

    last_error = ""
    for offset in range(lookback_days):
        date_str = (datetime.now(TAIPEI_TZ) - timedelta(days=offset)).strftime("%Y%m%d")
        url = (
            "https://www.twse.com.tw/rwd/zh/fund/T86"
            f"?response=json&date={date_str}&selectType=ALL"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            last_error = f"證交所連線失敗:{str(exc)[:80]}"
            continue

        if data.get("stat") != "OK":
            last_error = f"{date_str} 狀態:{data.get('stat')}"
            continue

        fields = data.get("fields", [])
        rows = data.get("data", [])
        idx_id = _find_field(fields, "證券代號")
        idx_foreign = (
            _find_exact_field(fields, "外陸資買賣超股數(不含外資自營商)")
            or _find_field(fields, "外陸資", "買賣超")
        )
        idx_invest = _find_exact_field(fields, "投信買賣超股數") or _find_field(fields, "投信", "買賣超")
        idx_dealer = _find_exact_field(fields, "自營商買賣超股數")
        idx_total = _find_exact_field(fields, "三大法人買賣超股數")

        if None in (idx_id, idx_foreign, idx_invest, idx_dealer):
            last_error = f"{date_str} 欄位格式異動"
            continue

        for row in rows:
            if str(row[idx_id]).strip() == stock_id:
                foreign = _parse_int(row[idx_foreign])
                invest = _parse_int(row[idx_invest])
                dealer = _parse_int(row[idx_dealer])
                total = _parse_int(row[idx_total]) if idx_total is not None else foreign + invest + dealer
                return {
                    "success": True,
                    "date": date_str,
                    "foreign_net": foreign,
                    "invest_net": invest,
                    "dealer_net": dealer,
                    "total_net": total,
                    "error": "",
                }
        last_error = f"{date_str} 找不到 {stock_id}"

    return {
        "success": False,
        "date": "",
        "foreign_net": 0,
        "invest_net": 0,
        "dealer_net": 0,
        "total_net": 0,
        "error": last_error or "無三大法人資料",
    }


def fetch_weekly_institutional(ticker: str, end_date: datetime | None = None, lookback_days: int = 10) -> dict:
    stock_id = ticker.upper().replace(".TW", "").replace(".TWO", "")
    if not stock_id.isdigit():
        return {
            "success": False,
            "date_range": "",
            "foreign_net": 0,
            "invest_net": 0,
            "dealer_net": 0,
            "total_net": 0,
            "days": 0,
            "error": "指數不適用三大法人個股買賣超",
        }

    base = end_date or datetime.now(TAIPEI_TZ)
    if base.tzinfo is None:
        base = base.replace(tzinfo=TAIPEI_TZ)
    week_start, week_end = _week_bounds(base)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.twse.com.tw/",
    }
    totals = {"foreign_net": 0, "invest_net": 0, "dealer_net": 0, "total_net": 0}
    hit_dates = []
    last_error = ""
    daily_records = []

    for offset in range(lookback_days):
        day = base.date() - timedelta(days=offset)
        if not (week_start <= day <= week_end):
            continue
        date_str = day.strftime("%Y%m%d")
        url = (
            "https://www.twse.com.tw/rwd/zh/fund/T86"
            f"?response=json&date={date_str}&selectType=ALL"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            last_error = f"{date_str} 證交所連線失敗:{str(exc)[:80]}"
            continue
        if data.get("stat") != "OK":
            last_error = f"{date_str} 狀態:{data.get('stat')}"
            continue

        fields = data.get("fields", [])
        idx_id = _find_field(fields, "證券代號")
        idx_foreign = (
            _find_exact_field(fields, "外陸資買賣超股數(不含外資自營商)")
            or _find_field(fields, "外陸資", "買賣超")
        )
        idx_invest = _find_exact_field(fields, "投信買賣超股數") or _find_field(fields, "投信", "買賣超")
        idx_dealer = _find_exact_field(fields, "自營商買賣超股數")
        idx_total = _find_exact_field(fields, "三大法人買賣超股數")
        if None in (idx_id, idx_foreign, idx_invest, idx_dealer):
            last_error = f"{date_str} 欄位格式異動"
            continue

        for row in data.get("data", []):
            if str(row[idx_id]).strip() != stock_id:
                continue
            foreign = _parse_int(row[idx_foreign])
            invest = _parse_int(row[idx_invest])
            dealer = _parse_int(row[idx_dealer])
            total = _parse_int(row[idx_total]) if idx_total is not None else foreign + invest + dealer
            totals["foreign_net"] += foreign
            totals["invest_net"] += invest
            totals["dealer_net"] += dealer
            totals["total_net"] += total
            daily_records.append({
                "date": date_str,
                "foreign_net": foreign,
                "invest_net": invest,
                "dealer_net": dealer,
                "total_net": total,
            })
            hit_dates.append(date_str)
            break

    if not hit_dates:
        return dict(success=False, date_range="", days=0, error=last_error or "本週無三大法人資料", **totals)

    hit_dates.sort()
    return dict(
        success=True,
        date_range=f"{hit_dates[0]}-{hit_dates[-1]}",
        days=len(hit_dates),
        error="",
        daily=sorted(daily_records, key=lambda x: x["date"]),
        **totals,
    )


def fetch_market_institutional_value_day(day) -> dict | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Referer": "https://www.twse.com.tw/",
    }
    date_str = day.strftime("%Y%m%d") if hasattr(day, "strftime") else str(day)
    url = f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&dayDate={date_str}&type=day"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    if data.get("stat") != "OK":
        return None
    fields = data.get("fields", [])
    rows = data.get("data", [])
    idx_name = _find_exact_field(fields, "單位名稱") or 0
    idx_net = _find_exact_field(fields, "買賣差額")
    if idx_net is None:
        return None
    totals = {"foreign": 0, "trust": 0, "dealer": 0, "total": 0}
    for row in rows:
        name = str(row[idx_name])
        net = _parse_int(row[idx_net])
        if "合計" in name:
            totals["total"] = net
        elif "外資及陸資" in name and "不含" in name:
            totals["foreign"] += net
        elif "投信" in name:
            totals["trust"] += net
        elif "自營商" in name:
            totals["dealer"] += net
    if not any(totals.values()):
        return None
    return {"date": date_str, **totals}


def fetch_market_institutional_value_week(end_date: datetime | None = None, lookback_days: int = 10) -> dict:
    base = end_date or datetime.now(TAIPEI_TZ)
    if base.tzinfo is None:
        base = base.replace(tzinfo=TAIPEI_TZ)
    week_start, week_end = _week_bounds(base)
    daily = []
    for offset in range(lookback_days):
        day = base.date() - timedelta(days=offset)
        if not (week_start <= day <= week_end):
            continue
        item = fetch_market_institutional_value_day(day)
        if item:
            daily.append(item)
    daily.sort(key=lambda x: x["date"])
    if not daily:
        return {"success": False, "daily": [], "foreign": 0, "trust": 0, "dealer": 0, "total": None, "error": "本週無三大法人金額資料"}
    return {
        "success": True,
        "daily": daily,
        "foreign": sum(x["foreign"] for x in daily),
        "trust": sum(x["trust"] for x in daily),
        "dealer": sum(x["dealer"] for x in daily),
        "total": sum(x["total"] for x in daily),
        "date_range": f"{daily[0]['date']}-{daily[-1]['date']}",
        "error": "",
    }


# ── 抓取資料 ────────────────────────────────────────────────

def _parse_twse_date(value: str):
    parts = str(value).strip().split("/")
    if len(parts) != 3:
        return None
    year = int(parts[0])
    if year < 1911:
        year += 1911
    return datetime(year, int(parts[1]), int(parts[2]))


def _month_starts(start_date, end_date) -> list:
    cur = datetime(start_date.year, start_date.month, 1).date()
    last = datetime(end_date.year, end_date.month, 1).date()
    months = []
    while cur <= last:
        months.append(cur)
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return months


def _twse_headers() -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Referer": "https://www.twse.com.tw/",
    }


def _fetch_twse_stock_data(stock_id: str, start_date, end_date) -> pd.DataFrame:
    rows = []
    for month in _month_starts(start_date, end_date):
        url = (
            "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
            f"?response=json&date={month.strftime('%Y%m%d')}&stockNo={stock_id}"
        )
        resp = requests.get(url, headers=_twse_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("stat") != "OK":
            continue
        fields = data.get("fields", [])
        idx_date = _find_field(fields, "日期")
        idx_volume = _find_field(fields, "成交股數")
        idx_open = _find_field(fields, "開盤價")
        idx_high = _find_field(fields, "最高價")
        idx_low = _find_field(fields, "最低價")
        idx_close = _find_field(fields, "收盤價")
        if None in (idx_date, idx_open, idx_high, idx_low, idx_close):
            continue
        for row in data.get("data", []):
            dt = _parse_twse_date(row[idx_date])
            if not dt or not (start_date <= dt.date() <= end_date):
                continue
            open_v = _parse_float(row[idx_open])
            high_v = _parse_float(row[idx_high])
            low_v = _parse_float(row[idx_low])
            close_v = _parse_float(row[idx_close])
            if None in (open_v, high_v, low_v, close_v):
                continue
            volume_v = _parse_float(row[idx_volume]) if idx_volume is not None else 0
            rows.append((dt, open_v, high_v, low_v, close_v, volume_v or 0))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"]).drop_duplicates("Date")
    return df.set_index("Date").sort_index()


def _fetch_twse_index_data(start_date, end_date) -> pd.DataFrame:
    rows = []
    for month in _month_starts(start_date, end_date):
        url = (
            "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST"
            f"?response=json&date={month.strftime('%Y%m%d')}"
        )
        resp = requests.get(url, headers=_twse_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("stat") != "OK":
            continue
        fields = data.get("fields", [])
        idx_date = _find_field(fields, "日期")
        idx_open = _find_field(fields, "開盤")
        idx_high = _find_field(fields, "最高")
        idx_low = _find_field(fields, "最低")
        idx_close = _find_field(fields, "收盤")
        if None in (idx_date, idx_open, idx_high, idx_low, idx_close):
            continue
        for row in data.get("data", []):
            dt = _parse_twse_date(row[idx_date])
            if not dt or not (start_date <= dt.date() <= end_date):
                continue
            open_v = _parse_float(row[idx_open])
            high_v = _parse_float(row[idx_high])
            low_v = _parse_float(row[idx_low])
            close_v = _parse_float(row[idx_close])
            if None in (open_v, high_v, low_v, close_v):
                continue
            rows.append((dt, open_v, high_v, low_v, close_v, 0))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"]).drop_duplicates("Date")
    return df.set_index("Date").sort_index()


def fetch_twse_closed_dates(years: set[int]) -> set[date]:
    closed_dates = set()
    for year in sorted(years):
        url = (
            "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"
            f"?response=json&queryYear={year - 1911}"
        )
        resp = requests.get(url, headers=_twse_headers(), timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if str(payload.get("stat", "")).lower() != "ok":
            raise RuntimeError(f"證交所 {year} 年休市日曆回傳異常：{payload.get('stat')}")
        closed_dates.update(parse_twse_closed_dates(payload))
    return closed_dates


def parse_twse_closed_dates(payload: dict) -> set[date]:
    closed_dates = set()
    for row in payload.get("data", []):
        if len(row) < 2 or "交易日" in str(row[1]):
            continue
        try:
            closed_dates.add(datetime.strptime(str(row[0]), "%Y-%m-%d").date())
        except ValueError:
            continue
    return closed_dates


def is_twse_trading_day(day: date, closed_dates: set[date]) -> bool:
    return day.weekday() < 5 and day not in closed_dates


def last_twse_trading_day_of_week(day: date, closed_dates: set[date]) -> date | None:
    monday = day - timedelta(days=day.weekday())
    for offset in range(4, -1, -1):
        candidate = monday + timedelta(days=offset)
        if is_twse_trading_day(candidate, closed_dates):
            return candidate
    return None


def latest_twse_trading_day(now: datetime, closed_dates: set[date]) -> date:
    dt = now if now.tzinfo else now.replace(tzinfo=TAIPEI_TZ)
    candidate = dt.date()
    if dt.time() < time(13, 40):
        candidate -= timedelta(days=1)
    for _ in range(370):
        if is_twse_trading_day(candidate, closed_dates):
            return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError("無法找到最近台股交易日")


def resolve_weekly_report_target(now: datetime, closed_dates: set[date]) -> date:
    dt = now if now.tzinfo else now.replace(tzinfo=TAIPEI_TZ)
    monday = dt.date() - timedelta(days=dt.date().weekday())
    for week_offset in range(0, 54):
        week_day = monday - timedelta(days=7 * week_offset)
        target = last_twse_trading_day_of_week(week_day, closed_dates)
        if target is None:
            continue
        due_at = datetime.combine(target, WEEKLY_REPORT_START_TIME, tzinfo=TAIPEI_TZ)
        if dt >= due_at:
            return target
    raise RuntimeError("無法找到已到產出時間的台股週報交易日")


def resolve_report_target(now: datetime, force_run: bool) -> date:
    dt = now if now.tzinfo else now.replace(tzinfo=TAIPEI_TZ)
    monday = dt.date() - timedelta(days=dt.date().weekday())
    calendar_years = {
        (monday - timedelta(days=7)).year,
        (monday + timedelta(days=4)).year,
    }
    try:
        closed_dates = fetch_twse_closed_dates(calendar_years)
    except Exception as exc:
        raise RuntimeError(
            f"無法取得證交所官方休市日曆，為避免選錯週報日，本次中止並等待下次重試：{exc}"
        ) from exc
    if force_run:
        return latest_twse_trading_day(dt, closed_dates)
    return resolve_weekly_report_target(dt, closed_dates)


def _expected_latest_price_date(now: datetime | None = None):
    dt = now or datetime.now(TAIPEI_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI_TZ)
    day = dt.date()
    if day.weekday() >= 5:
        return day - timedelta(days=day.weekday() - 4)
    if dt.time() >= time(13, 40):
        return day
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def _is_fresh_price_data(df: pd.DataFrame, end_date, max_stale_days: int = 0) -> bool:
    if df is None or df.empty:
        return False
    latest = _date_only(df.index[-1])
    return latest >= (end_date - timedelta(days=max_stale_days))



def fetch_data(ticker: str, days: int, end_date: date | None = None) -> pd.DataFrame:
    # 台股價格優先用證交所官方日資料，避免 yfinance 調整價或暫存造成收盤價失真。
    now_tw = datetime.now(TAIPEI_TZ)
    end = end_date or _expected_latest_price_date(now_tw)
    start = end - timedelta(days=days)
    stock_id = ticker.upper().replace(".TW", "").replace(".TWO", "")
    try:
        if ticker == "^TWII":
            twse_df = _fetch_twse_index_data(start, end)
        elif stock_id.isdigit() and ticker.upper().endswith(".TW"):
            twse_df = _fetch_twse_stock_data(stock_id, start, end)
        else:
            twse_df = pd.DataFrame()
        if not twse_df.empty:
            twse_df = twse_df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])
            if _is_fresh_price_data(twse_df, end):
                return twse_df
            latest = twse_df.index[-1].strftime("%Y-%m-%d")
            print(f"⚠️  證交所官方價格資料過舊：{ticker} 最新僅到 {latest}，預期至少 {end}，改用 yfinance 備援")
    except Exception as exc:
        print(f"⚠️  證交所官方價格資料失敗，改用 yfinance 備援：{ticker} {str(exc)[:80]}")

    # yfinance 的 end 是「不含當日」的結束日期；收盤後要抓到今天資料，必須設成台灣明天。
    yf_end = end + timedelta(days=1)
    yf_start = yf_end - timedelta(days=days)
    df = yf.download(ticker,
                     start=yf_start.strftime("%Y-%m-%d"),
                     end=yf_end.strftime("%Y-%m-%d"),
                     progress=False, auto_adjust=False)
    if df.empty:
        raise ValueError(f"無法取得 {ticker} 資料")
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if not _is_fresh_price_data(df, end):
        latest = df.index[-1].strftime("%Y-%m-%d")
        raise ValueError(f"{ticker} 價格資料過舊，最新僅到 {latest}，預期至少 {end}")
    return df


def _fetch_close_series(ticker: str, days: int = 180) -> pd.Series:
    end   = datetime.now(TAIPEI_TZ).date() + timedelta(days=1)
    start = end - timedelta(days=days)
    df = yf.download(ticker,
                     start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"),
                     progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"無法取得 {ticker} 資料")
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df["Close"].dropna()


def _series_change_pct(series: pd.Series, periods: int) -> float | None:
    if len(series) <= periods:
        return None
    prev = float(series.iloc[-1 - periods])
    if prev == 0:
        return None
    return (float(series.iloc[-1]) - prev) / prev * 100


def _date_only(value):
    if hasattr(value, "date"):
        return value.date()
    return pd.Timestamp(value).date()


def _week_bounds(reference_date):
    ref = _date_only(reference_date)
    monday = ref - timedelta(days=ref.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def _current_week_series(series: pd.Series, reference_date=None, max_points: int = 5) -> list[float]:
    if series is None or series.empty:
        return []
    ref = reference_date or series.index[-1]
    monday, friday = _week_bounds(ref)
    picked = []
    for idx, value in series.dropna().items():
        day = _date_only(idx)
        if monday <= day <= friday:
            picked.append(float(value))
    if len(picked) >= 2:
        return picked[-max_points:]
    return [float(x) for x in series.dropna().tail(max_points)]


def _cumulative(values: list[float]) -> list[float]:
    total = 0.0
    out = []
    for value in values or []:
        try:
            total += float(value)
            out.append(total)
        except Exception:
            continue
    return out


def fetch_market_context(reference_date=None) -> dict:
    """
    抓取每日會影響台股風險偏好的總體資料。
    USD/TWD 上升代表美元變貴、台幣轉弱；美債殖利率上升代表估值壓力提高。
    """
    context = {"success": True, "fx": None, "rates": None, "errors": []}

    try:
        fx = _fetch_close_series("TWD=X", 180)
        context["fx"] = {
            "ticker": "TWD=X",
            "label": "美元/台幣",
            "value": float(fx.iloc[-1]),
            "chg_5d_pct": _series_change_pct(fx, 5),
            "chg_20d_pct": _series_change_pct(fx, 20),
            "series": _current_week_series(fx, reference_date),
            "source": "Yahoo Finance TWD=X",
        }
    except Exception as exc:
        context["success"] = False
        context["errors"].append(f"匯率資料失敗:{str(exc)[:80]}")

    try:
        rates = _fetch_close_series("^TNX", 180)
        current = float(rates.iloc[-1])
        context["rates"] = {
            "ticker": "^TNX",
            "label": "美國10年期公債殖利率",
            "value": current,
            "chg_5d_bp": (current - float(rates.iloc[-6])) * 100 if len(rates) > 5 else None,
            "chg_20d_bp": (current - float(rates.iloc[-21])) * 100 if len(rates) > 20 else None,
            "series": _current_week_series(rates, reference_date),
            "source": "Yahoo Finance ^TNX",
        }
    except Exception as exc:
        context["success"] = False
        context["errors"].append(f"利率資料失敗:{str(exc)[:80]}")

    return context


