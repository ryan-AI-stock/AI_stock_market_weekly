"""
每週台股趨勢訊號系統 v1
===================
Repository : github.com/ryanhsu1983/AI_stock_market_weekly
從每日版模型改造為週報：追蹤台股加權與中大型權值股的本週變化、趨勢判斷與下週觀察。
"""

import html as html_lib
import os, re, sys, requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import yfinance as yf
import pandas as pd
from datetime import date, datetime, timedelta, timezone, time
from pathlib import Path

from weekly_drive_client import build_google_drive_service, drive_name_query, upload_file_to_drive
from weekly_drive_settings import (
    in_acceptance_drive_mode,
    resolve_backup_drive_folder_id,
    resolve_public_report_file_id,
    resolve_public_report_folder_id,
)
from weekly_email import send_report_email
from weekly_publish_policy import email_disabled, handle_drive_publish_failure
from weekly_runtime import env_flag, load_config_file, write_github_output

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CACHE_DIR = Path(__file__).parent / ".yfinance_cache"
CACHE_DIR.mkdir(exist_ok=True)
yf.cache.set_cache_location(str(CACHE_DIR))

UP_COLOR = "#c0392b"
DOWN_COLOR = "#168f4d"
WARN_COLOR = "#e67e22"
INFO_COLOR = "#3498db"
NEUTRAL_COLOR = "#95a5a6"
TAIPEI_TZ = timezone(timedelta(hours=8))
WEEKLY_REPORT_START_TIME = time(15, 0)
WEEKLY_DARK = "#12322b"
WEEKLY_DARK_2 = "#1f493f"
WEEKLY_GOLD = "#c9a227"
WEEKLY_BG = "#f4f2ea"
WEEKLY_PANEL = "#fffdf7"


def get_report_meta(report_date: datetime | None = None) -> dict:
    dt = report_date or datetime.now(TAIPEI_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI_TZ)
    iso = dt.date().isocalendar()
    return {
        "date": dt.strftime("%Y-%m-%d"),
        "date_key": dt.strftime("%Y%m%d"),
        "year": dt.strftime("%Y"),
        "week": iso.week,
        "week_key": f"{iso.year}-W{iso.week:02d}",
        "week_label": f"第{iso.week}週",
    }


def pct_text(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%"

WEIGHTS = {
    "trend": 25,
    "macd": 20,
    "institutional": 15,
    "kd": 12,
    "obv": 8,
    "fx": 7,
    "rates": 7,
    "vol": 6,
}

SIGNAL_LEVELS = [
    (70, "STRONG", "強訊號"),
    (50, "MID", "中訊號"),
    (30, "WEAK", "弱訊號"),
    (15, "NOTICE", "提醒"),
    (0, "NEUTRAL", "無訊號"),
]

TRADE_BASE_PCTS = {
    "STRONG": 50,
    "MID": 40,
    "WEAK": 10,
    "NOTICE": 0,
    "NEUTRAL": 0,
}


# ── 讀取設定 ────────────────────────────────────────────────
def load_config() -> dict:
    return load_config_file()


def get_stock_cfg(stock: dict, global_cfg: dict) -> dict:
    """
    將全域設定與個股 overrides 合併，個股設定優先。
    回傳該股票實際使用的完整設定。
    """
    ov  = stock.get("overrides", {})
    thr = dict(global_cfg["thresholds"])
    ma  = dict(global_cfg["ma_periods"])

    # 覆蓋 thresholds
    for key in ("kd_buy","kd_sell","bias20_buy","bias20_sell",
                "bias60_p_low","bias60_p_high","vol_ma_period","obv_ma_period"):
        if key in ov:
            thr[key] = ov[key]

    # 向下相容舊欄位名稱
    if "bias_buy"  in thr and "bias20_buy"  not in thr: thr["bias20_buy"]  = thr["bias_buy"]
    if "bias_sell" in thr and "bias20_sell" not in thr: thr["bias20_sell"] = thr["bias_sell"]

    # 覆蓋 ma_periods
    if "ma_periods" in ov:
        ma.update(ov["ma_periods"])

    return {
        "thresholds":       thr,
        "ma_periods":       ma,
        "pyramid":          global_cfg.get("pyramid", {}),
        "use_obv":          ov.get("use_obv",          True),
        "use_vol_trend":    ov.get("use_vol_trend",     True),
        "use_institutional":ov.get("use_institutional", True),
        "use_fx":           ov.get("use_fx",            True),
        "use_rates":        ov.get("use_rates",         True),
        "macro_sensitivity": ov.get("macro_sensitivity", "market"),
        "leverage_warning": ov.get("leverage_warning",  False),
        "bias60_locked":    ov.get("bias60_locked",     True),
    }


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


# ── 計算指標 ────────────────────────────────────────────────
def calc_indicators(df: pd.DataFrame, scfg: dict) -> pd.DataFrame:
    ma  = scfg["ma_periods"]
    thr = scfg["thresholds"]
    s, m, l = ma["short"], ma["mid"], ma["long"]

    df[f"MA{s}"] = df["Close"].rolling(s).mean()
    df[f"MA{m}"] = df["Close"].rolling(m).mean()
    df[f"MA{l}"] = df["Close"].rolling(l).mean()

    # BIAS60（季線乖離，固定60日，用於Z-Score）
    ma60         = df["Close"].rolling(60).mean()
    df["BIAS60"] = (df["Close"] - ma60) / ma60 * 100
    b60_clean    = df["BIAS60"].dropna()
    p_low        = thr.get("bias60_p_low",  5)
    p_high       = thr.get("bias60_p_high", 95)
    df.attrs["bias60_p_high"] = float(b60_clean.quantile(p_high / 100))
    df.attrs["bias60_p_low"]  = float(b60_clean.quantile(p_low  / 100))
    df.attrs["bias60_mean"]   = float(b60_clean.mean())
    df.attrs["bias60_std"]    = float(b60_clean.std())
    df["BIAS60_Z"] = (df["BIAS60"] - df.attrs["bias60_mean"]) / df.attrs["bias60_std"]

    # 短線乖離率（依各股 mid MA）
    df["Bias20"] = (df["Close"] - df[f"MA{m}"]) / df[f"MA{m}"] * 100

    # KD
    low_min  = df["Low"].rolling(9).min()
    high_max = df["High"].rolling(9).max()
    rsv      = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["K"]  = rsv.ewm(com=2, adjust=False).mean()
    df["D"]  = df["K"].ewm(com=2, adjust=False).mean()

    # MACD
    ema12           = df["Close"].ewm(span=12, adjust=False).mean()
    ema26           = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"]       = ema12 - ema26
    df["Signal"]    = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["DIF"] - df["Signal"]

    # 量能趨勢
    vp           = thr["vol_ma_period"]
    df["Vol_MA"] = df["Volume"].rolling(vp).mean()
    df["Vol_Trend"] = df["Vol_MA"] - df["Vol_MA"].shift(3)

    # OBV
    obv = [0]
    for i in range(1, len(df)):
        if   df["Close"].iloc[i] > df["Close"].iloc[i-1]: obv.append(obv[-1] + df["Volume"].iloc[i])
        elif df["Close"].iloc[i] < df["Close"].iloc[i-1]: obv.append(obv[-1] - df["Volume"].iloc[i])
        else:                                               obv.append(obv[-1])
    df["OBV"]    = obv
    df["OBV_MA"] = df["OBV"].rolling(thr["obv_ma_period"]).mean()

    return df


# ── BIAS60 Z-Score 評估 ──────────────────────────────────────
def eval_bias60(df: pd.DataFrame, scfg: dict) -> dict:
    latest  = df.iloc[-1]
    bias60  = float(latest["BIAS60"])
    z       = float(latest["BIAS60_Z"])
    p_high  = df.attrs["bias60_p_high"]
    p_low   = df.attrs["bias60_p_low"]
    p_high_pct = scfg["thresholds"].get("bias60_p_high", 95)
    p_low_pct  = scfg["thresholds"].get("bias60_p_low",   5)
    can_lock   = scfg.get("bias60_locked", True)

    if bias60 >= p_high:
        zone   = "overheated"
        locked = can_lock
        label  = f"🔥 過熱{'鎖定' if can_lock else '警示'}（季線乖離{bias60:.1f}%，歷史{p_high_pct}%分位）"
        color  = UP_COLOR
        note   = f"Z={z:.2f}｜超過歷史{p_high_pct}%分位({p_high:.1f}%)｜{'正向條件暫停計入' if can_lock else '僅警示，不鎖定'}"
    elif bias60 <= p_low:
        zone   = "oversold"
        locked = False
        label  = f"❄️ 超跌觀察區（季線乖離{bias60:.1f}%，歷史{p_low_pct}%分位）"
        color  = DOWN_COLOR
        note   = f"Z={z:.2f}｜低於歷史{p_low_pct}%分位({p_low:.1f}%)｜統計超跌觀察區"
    else:
        zone   = "normal"
        locked = False
        label  = f"正常範圍（季線乖離{bias60:.1f}%）"
        color  = NEUTRAL_COLOR
        note   = f"Z={z:.2f}｜介於{p_low_pct}%({p_low:.1f}%)～{p_high_pct}%({p_high:.1f}%)分位之間"

    return dict(zone=zone, locked=locked, bias60=bias60,
                z_score=z, p_high=p_high, p_low=p_low,
                label=label, color=color, note=note)


# ── 金字塔建倉計算 ───────────────────────────────────────────
def calc_pyramid(df: pd.DataFrame, scfg: dict, signal_level: str) -> dict:
    py         = scfg.get("pyramid", {})
    drop_step  = py.get("add_per_drop_pct",    5.0)
    add_ratio  = py.get("add_ratio_pct",       20.0)
    time_days  = py.get("time_rebalance_days", 20)
    time_ratio = py.get("time_add_ratio_pct",   5.0)

    close    = float(df["Close"].iloc[-1])
    recent   = df["Close"].iloc[-time_days:]
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


def score_to_signal(score: float) -> tuple:
    for threshold, key, label in SIGNAL_LEVELS:
        if score >= threshold:
            return key, label
    return "NEUTRAL", "無訊號"


def classify_market_regime(close: float, ma_s: float, ma_m: float, ma_l: float,
                           ma_s_prev: float, ma_m_prev: float, ma_l_prev: float) -> dict:
    ma_s_up = ma_s > ma_s_prev
    ma_m_up = ma_m > ma_m_prev
    ma_l_up = ma_l > ma_l_prev

    if ma_m > ma_l and close > ma_m and ma_s_up and ma_m_up and ma_l_up:
        return {
            "key": "STRONG_BULL",
            "label": "大多頭",
            "color": UP_COLOR,
            "note": "中期均線維持多頭排列，價格也站在主要均線上方；短線風險條件增加時，仍需搭配關鍵均線判斷趨勢是否改變。",
        }
    if ma_m > ma_l:
        return {
            "key": "BULL_PULLBACK",
            "label": "多頭修正",
            "color": WARN_COLOR,
            "note": "中期仍是多頭，但短線轉弱或跌回均線附近；此時適合觀察是否回到支撐，而不是把它直接當成空頭。",
        }
    if ma_m < ma_l and close < ma_m:
        return {
            "key": "BEAR",
            "label": "空頭",
            "color": DOWN_COLOR,
            "note": "中期均線偏空且價格落在主要均線下方；此時風險條件權重提高，正向條件需更審慎解讀。",
        }
    return {
        "key": "RANGE",
        "label": "盤整",
        "color": NEUTRAL_COLOR,
        "note": "趨勢方向尚未明確；此時需搭配多項條件判斷，不宜過度解讀單一弱訊號。",
    }


def _parse_signal_level(level: str) -> tuple:
    if level.startswith("BUY_"):
        return "BUY", level.replace("BUY_", "")
    if level.startswith("SELL_"):
        return "SELL", level.replace("SELL_", "")
    if level.startswith("OVERHEATED_"):
        return "OVERHEATED", level.replace("OVERHEATED_", "")
    return "HOLD", "NEUTRAL"


def _phrase_list(items: list[str], limit: int = 3) -> str:
    cleaned = []
    for item in items:
        item = str(item or "").strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return "、".join(cleaned[:limit])


def _build_contextual_reason(direction: str, level_key: str, regime: dict,
                             b60: dict, context: dict | None) -> str | None:
    if not context:
        return None
    positives = _phrase_list(context.get("positive_evidence", []), 3)
    risks = _phrase_list(context.get("risk_evidence", []), 3)
    regime_key = regime.get("key", "")
    ma60_dist = context.get("ma60_dist")
    recent_low_ma60_dist = context.get("recent_low_ma60_dist")
    near_ma60 = (
        ma60_dist is not None and abs(ma60_dist) <= 5
    ) or (
        recent_low_ma60_dist is not None and recent_low_ma60_dist <= 2
    )

    if direction == "BUY":
        if near_ma60 and regime_key in ("BULL_PULLBACK", "RANGE"):
            return "目前較像季線附近的支撐反彈，" + (
                f"{positives}是正面訊號，" if positives else ""
            ) + "但仍要看量能與法人是否延續，才算趨勢重新轉強。"
        if regime_key == "BEAR":
            return "空頭或中期趨勢尚未修復時，反彈先視為修正中的反彈；正向條件僅供觀察，追價風險偏高。"
        if b60.get("zone") == "overheated":
            return "中期乖離偏高，代表安全邊際下降；即使動能仍強，也需觀察拉回風險。"
        if positives and risks:
            return f"{positives}偏正面，但{risks}仍需修復；仍需等待確認，追價風險仍在。"
        if positives:
            return f"{positives}支持趨勢延續，但週報模型仍以分批和安全邊際為主。"

    if direction == "SELL":
        if regime_key in ("STRONG_BULL", "BULL_PULLBACK") and level_key in ("WEAK", "NOTICE"):
            return "多頭修正中的輕度風險條件偏向提醒；重點觀察是否跌破關鍵均線。"
        if risks:
            return f"{risks}顯示風險升溫；後續需觀察法人與量能是否改善。"

    if direction == "OVERHEATED":
        return "過熱代表追價風險高，不代表趨勢一定結束；後續重點是觀察拉回與趨勢變化。"

    if regime_key == "RANGE":
        if risks:
            return f"目前屬於盤整區間，{risks}使整理偏保守；先看區間高低點是否突破。"
        return "目前屬於盤整區間，方向尚在累積；若法人與量能穩定，整理仍可視為良性。"
    return None


def build_trade_plan(level: str, regime: dict, b60: dict, lev_warn: bool = False,
                     context: dict | None = None) -> dict:
    direction, level_key = _parse_signal_level(level)
    base_pct = TRADE_BASE_PCTS.get(level_key, 0)
    regime_key = regime["key"]
    action = "條件待確認"
    trade_pct = 0
    color = NEUTRAL_COLOR
    headline = "維持觀察"
    reason = "目前訊號不足，保留觀察即可。"

    if direction == "BUY":
        action = "正向條件"
        color = UP_COLOR
        if regime_key == "BEAR":
            trade_pct = {"STRONG": 20, "MID": 10, "WEAK": 0, "NOTICE": 0, "NEUTRAL": 0}.get(level_key, 0)
            reason = "空頭環境下即使正向條件增加，也先視為反彈觀察，仍需等待趨勢修復。"
        elif regime_key == "STRONG_BULL" and b60["zone"] == "overheated":
            trade_pct = 0
            action = "追價風險偏高"
            color = WARN_COLOR
            reason = "趨勢條件仍成立，但季線乖離已高，追價風險偏高。"
        elif regime_key == "STRONG_BULL":
            trade_pct = base_pct
            reason = "大多頭環境下正向條件較完整，仍需觀察訊號是否持續或升級。"
        elif regime_key == "BULL_PULLBACK":
            trade_pct = base_pct
            reason = "多頭修正中的正向條件增加，但仍需觀察後續是否持續修復。"
        else:
            trade_pct = base_pct
            reason = "盤整環境下方向尚未明確，需等待更多條件確認。"

    elif direction == "SELL":
        action = "風險條件"
        color = DOWN_COLOR
        if regime_key == "STRONG_BULL":
            trade_pct = {"STRONG": 30, "MID": 10, "WEAK": 0, "NOTICE": 0, "NEUTRAL": 0}.get(level_key, 0)
            reason = "大多頭下輕度風險條件通常只是震盪提醒；條件增加時再檢查趨勢是否改變。"
        elif regime_key == "BULL_PULLBACK":
            trade_pct = {"STRONG": 40, "MID": 20, "WEAK": 0, "NOTICE": 0, "NEUTRAL": 0}.get(level_key, 0)
            reason = "多頭修正時，輕度風險條件不代表趨勢已反轉；需觀察風險條件是否持續增加。"
        elif regime_key == "BEAR":
            trade_pct = base_pct
            reason = "空頭環境下風險條件可信度提高，需留意風險是否持續擴大。"
        else:
            trade_pct = base_pct
            reason = "盤整環境下需避免過度解讀單日條件，持續觀察區間變化。"

    elif direction == "OVERHEATED":
        action = "追價風險偏高"
        color = WARN_COLOR
        if level_key in ("MID", "STRONG"):
            if regime_key == "STRONG_BULL":
                trade_pct = 10 if level_key == "MID" else 30
                reason = "行情仍屬大多頭，但已過熱且風險條件分數升高；需留意波動與槓桿風險。"
            elif regime_key == "BULL_PULLBACK":
                trade_pct = 20 if level_key == "MID" else 40
                reason = "過熱後進入修正，風險條件分數已不低，需觀察整理是否延續。"
            else:
                trade_pct = base_pct
                reason = "過熱且風險條件明顯，後續需留意波動擴大。"
            action = "風險條件"
            color = DOWN_COLOR
        else:
            trade_pct = 0
            reason = "過熱代表追價風險偏高；但風險條件分數仍不高，需搭配趨勢變化觀察。"

    if level_key == "NOTICE":
        trade_pct = 0
        reason = "提醒等級只代表市場溫度有變化，僅供條件觀察。"

    contextual_reason = _build_contextual_reason(direction, level_key, regime, b60, context)
    if contextual_reason:
        reason = contextual_reason

    if lev_warn and trade_pct > 0:
        trade_pct = min(trade_pct, 20)
        reason += " 槓桿ETF波動與耗損較高，條件分數上限採較保守設定。"

    if trade_pct > 0:
        headline = f"{action}通過 {int(round(trade_pct / 10))}/10"
    elif action == "追價風險偏高":
        headline = "追價風險偏高"
    else:
        headline = "維持觀察"

    return {
        "headline": headline,
        "action": action,
        "trade_pct": trade_pct,
        "base_pct": base_pct,
        "color": color,
        "reason": reason,
        "regime": regime,
        "repeat_rule": "同一等級條件連續出現時，不代表狀態改變；只有等級升降或條件變化時再重新評估。",
    }


def classify_weekly_posture(regime: dict, b60: dict, week_chg_pct: float | None,
                            close: float, ma_s: float, ma_m: float, ma_l: float,
                            effective_buy: float, effective_sell: float) -> tuple[str, str, str]:
    if b60.get("zone") == "overheated":
        return "過熱風險偏高", WARN_COLOR, "趨勢仍可偏多看待，但季線乖離偏高；下週重點是量縮拉回或高檔爆量轉弱。"
    if close > ma_s > ma_m > ma_l and (week_chg_pct or 0) > 0 and effective_buy >= effective_sell:
        return "趨勢條件仍成立", UP_COLOR, "價格站在主要均線上方且本週收高；下週觀察能否守住10日線並延續法人買超。"
    if close < ma_m and effective_sell >= max(30, effective_buy):
        return "轉弱觀察", DOWN_COLOR, "收盤跌回中短均線下方且風險分數升溫；下週先看20日線能否重新站回。"
    if week_chg_pct is not None and week_chg_pct < -3 and close >= ma_l:
        return "修正等待", WARN_COLOR, "本週拉回但尚未跌破季線；下週觀察量能是否收斂，以及是否出現止跌K線。"
    if regime.get("key") == "RANGE" or abs(week_chg_pct or 0) < 1.5:
        if close >= ma_s and close >= ma_m and effective_buy >= effective_sell:
            return (
                "良性盤整",
                INFO_COLOR,
                "目前屬於高檔整理而非明顯轉弱；法人與量能若能維持穩定，下週可觀察本週高點能否帶量突破，偏向多方續航整理。"
            )
        if close < ma_m or effective_sell >= effective_buy + 10:
            return (
                "防守盤整",
                WARN_COLOR,
                "目前進入整理但風險條件偏高；若法人續賣、量能放大且跌破本週低點，需提高防守意識，先觀察20日線與本週低點支撐。"
            )
        return (
            "中性盤整",
            NEUTRAL_COLOR,
            "目前屬於區間整理階段，市場正在消化宏觀變數與資金流向；下週以本週高低點、法人買賣超與匯率變化作為方向確認。"
        )
    if effective_buy > effective_sell:
        return "續強觀察", UP_COLOR, "多方條件仍優於風險條件；下週觀察本週高點能否帶量突破。"
    return "修正等待", WARN_COLOR, "風險條件略占上風；下週先觀察支撐與法人賣壓是否收斂。"


def range_position_note(value: float | None) -> str:
    if value is None:
        return "資料不足。"
    if value >= 80:
        return "越接近100%代表越靠近本週高點；目前收在偏高區，代表週末前買盤承接較強，但追價要留意震盪。"
    if value >= 60:
        return "越接近100%代表越靠近本週高點；目前收在中上區，短線仍有支撐。"
    if value >= 40:
        return "0%代表本週低點、100%代表本週高點；目前約在區間中段，方向尚未明顯表態。"
    if value >= 20:
        return "越接近0%代表越靠近本週低點；目前收在中下區，表示賣壓仍需要觀察。"
    return "越接近0%代表越靠近本週低點；目前收在偏低區，代表週末前賣壓偏重。"


def week_gap_note(weekly: dict) -> str:
    prev_close = weekly.get("prev_close")
    week_open = weekly.get("week_start_open")
    if not prev_close or not week_open:
        return "本週漲跌以週一開盤到週五收盤計算；相對上週五則是跨週持有者的報酬口徑。"
    gap_pct = weekly.get("week_open_gap_pct")
    if gap_pct is None:
        gap_pct = (week_open - prev_close) / prev_close * 100
    if abs(gap_pct) < 0.3:
        return "週一開盤與上週五收盤差距不大，所以本週漲跌與相對上週五通常會接近。"
    direction = "跳空開高" if gap_pct > 0 else "跳空開低"
    impact = "拉高跨週持有者報酬，但週內仍可能從高檔拉回" if gap_pct > 0 else "壓低跨週持有者報酬，即使週內反彈也可能仍偏弱"
    return f"週一相對上週五{direction}{pct_text(gap_pct)}；因此本週漲跌與相對上週五會不同，代表{impact}。"


def build_weekly_metrics(df: pd.DataFrame, scfg: dict, inst_week: dict | None,
                         regime: dict, b60: dict, effective_buy: float,
                         effective_sell: float) -> dict:
    ma = scfg["ma_periods"]
    s, m, l = ma["short"], ma["mid"], ma["long"]
    latest = df.iloc[-1]
    close = float(latest["Close"])

    latest_date = _date_only(df.index[-1])
    week_start_date, planned_week_end = _week_bounds(latest_date)
    week_mask = [week_start_date <= _date_only(idx) <= planned_week_end for idx in df.index]
    week = df.loc[week_mask]
    if week.empty:
        week = df.tail(5)
        week_start_date = _date_only(week.index[0])
        planned_week_end = _date_only(week.index[-1])

    week_end_date = _date_only(week.index[-1])
    prior = df.loc[[_date_only(idx) < week_start_date for idx in df.index]]
    prev_close = float(prior["Close"].iloc[-1]) if not prior.empty else None
    week_start_open = float(week["Open"].iloc[0]) if "Open" in week.columns and pd.notna(week["Open"].iloc[0]) else float(week["Close"].iloc[0])
    week_start_close = float(week["Close"].iloc[0])

    week_chg = close - week_start_open if week_start_open else None
    week_chg_pct = (week_chg / week_start_open * 100) if week_start_open else None
    prev_close_chg = close - prev_close if prev_close else None
    prev_close_chg_pct = (prev_close_chg / prev_close * 100) if prev_close else None
    week_open_gap_pct = ((week_start_open - prev_close) / prev_close * 100) if prev_close else None

    week_high = float(week["High"].max())
    week_low = float(week["Low"].min())
    week_volume = float(week["Volume"].sum())
    avg_volume_20 = float(df["Volume"].tail(20).mean()) if len(df) >= 20 else float(df["Volume"].mean())
    week_avg_volume = week_volume / max(len(week), 1)
    volume_ratio = week_avg_volume / avg_volume_20 if avg_volume_20 else None
    ma_values = {
        f"MA{s}": float(latest[f"MA{s}"]),
        f"MA{m}": float(latest[f"MA{m}"]),
        f"MA{l}": float(latest[f"MA{l}"]),
    }
    ma_position = " / ".join(
        f"{key}{'上' if close >= value else '下'}{(close - value) / value * 100:+.1f}%"
        for key, value in ma_values.items()
    )
    posture, color, next_focus = classify_weekly_posture(
        regime, b60, week_chg_pct, close, ma_values[f"MA{s}"], ma_values[f"MA{m}"], ma_values[f"MA{l}"],
        effective_buy, effective_sell
    )
    range_pos = (close - week_low) / (week_high - week_low) * 100 if week_high != week_low else 50.0
    range_note = range_position_note(range_pos)
    trend_summary = (
        f"{posture}｜週一開盤至週五收盤{pct_text(week_chg_pct)}，"
        f"收盤位置{range_pos:.0f}%（0%=本週低點、100%=本週高點）"
    )

    inst_total = inst_week.get("total_net") if inst_week and inst_week.get("success") else None
    inst_value = inst_total * close if inst_total is not None else None
    inst_daily_amounts = []
    if inst_week and inst_week.get("daily"):
        for day_item in inst_week.get("daily", []):
            shares = day_item.get("total_net", 0)
            inst_daily_amounts.append(float(shares) * close)

    weekday_names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    week_chart_points = []
    if not week.empty:
        first_day = _date_only(week.index[0])
        week_chart_points.append({
            "date": f"{weekday_names[first_day.weekday()]}開",
            "value": week_start_open,
            "kind": "open",
        })
        for idx, row in week.iterrows():
            day = _date_only(idx)
            week_chart_points.append({
                "date": weekday_names[day.weekday()],
                "value": float(row["Close"]),
                "kind": "close",
            })

    chart_rows = df.tail(60)
    chart_points = []
    for idx, row in chart_rows.iterrows():
        day = _date_only(idx)
        chart_points.append({
            "date": idx.strftime("%m/%d") if hasattr(idx, "strftime") else str(idx),
            "close": float(row["Close"]),
            "ma_s": float(row[f"MA{s}"]) if pd.notna(row[f"MA{s}"]) else None,
            "ma_m": float(row[f"MA{m}"]) if pd.notna(row[f"MA{m}"]) else None,
            "ma_l": float(row[f"MA{l}"]) if pd.notna(row[f"MA{l}"]) else None,
            "volume": float(row["Volume"]),
            "is_current_week": week_start_date <= day <= planned_week_end,
        })

    return {
        "week_start_date": week_start_date.strftime("%Y-%m-%d"),
        "week_end_date": week_end_date.strftime("%Y-%m-%d"),
        "week_range_label": f"{week_start_date.strftime('%m/%d')} - {week_end_date.strftime('%m/%d')}",
        "week_start_open": week_start_open,
        "week_start_close": week_start_close,
        "prev_close": prev_close,
        "prev_close_chg": prev_close_chg,
        "prev_close_chg_pct": prev_close_chg_pct,
        "week_open_gap_pct": week_open_gap_pct,
        "week_chg": week_chg,
        "week_chg_pct": week_chg_pct,
        "week_high": week_high,
        "week_low": week_low,
        "week_volume": week_volume,
        "week_avg_volume": week_avg_volume,
        "avg_volume_20": avg_volume_20,
        "volume_ratio": volume_ratio,
        "institutional_week": inst_week,
        "institutional_total": inst_total,
        "institutional_value": inst_value,
        "institutional_value_text": format_twd_billion_short(inst_value),
        "institutional_daily_values": _cumulative(inst_daily_amounts),
        "ma_position": ma_position,
        "posture": posture,
        "posture_color": color,
        "trend_summary": trend_summary,
        "next_focus": next_focus,
        "range_pos": range_pos,
        "range_position_note": range_note,
        "chart_points": chart_points,
        "week_chart_points": week_chart_points,
    }


def trade_plan_html(result: dict, compact: bool = False) -> str:
    trade_plan = result.get("trade_plan", {})
    if not trade_plan:
        return ""

    regime = trade_plan.get("regime", result.get("regime", {}))
    signal_badge = (
        f'<span style="background:{result.get("border", NEUTRAL_COLOR)};color:#fff;'
        f'font-size:12px;font-weight:bold;padding:4px 8px;border-radius:5px;'
        f'white-space:nowrap;display:inline-block;margin-right:6px;">'
        f'{result.get("summary", "無訊號")}</span>'
    )
    status_tags = (
        f'<span style="background:{regime.get("color", NEUTRAL_COLOR)};color:#fff;'
        f'font-size:12px;font-weight:bold;padding:4px 8px;border-radius:5px;'
        f'white-space:nowrap;display:inline-block;margin-right:6px;">'
        f'{regime.get("label", "市場狀態不明")}</span>'
    )
    if result.get("b60", {}).get("zone") == "overheated":
        status_tags += (
            f'<span style="background:#c0392b;color:#fff;font-size:12px;'
            f'font-weight:bold;padding:4px 8px;border-radius:5px;white-space:nowrap;'
            f'display:inline-block;margin-right:6px;">過熱鎖定</span>'
        )
    elif result.get("b60", {}).get("zone") == "oversold":
        status_tags += (
            f'<span style="background:#2980b9;color:#fff;font-size:12px;'
            f'font-weight:bold;padding:4px 8px;border-radius:5px;white-space:nowrap;'
            f'display:inline-block;margin-right:6px;">超跌區</span>'
        )

    margin = "margin-top:8px;" if compact else ""
    return (
        f'<div style="{margin}background:#fff;border:1px solid #eee;border-left:5px solid '
        f'{trade_plan.get("color", NEUTRAL_COLOR)};border-radius:8px;padding:10px 12px;">'
        f'<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:7px;">'
        f'{signal_badge}'
        f'{status_tags}'
        f'<span style="color:{trade_plan.get("color", NEUTRAL_COLOR)};'
        f'font-size:15px;font-weight:bold;">{trade_plan.get("headline", "維持觀察")}</span>'
        f'</div>'
        f'<div style="font-size:12px;color:#555;line-height:1.7;">'
        f'{trade_plan.get("reason", "")}</div>'
        f'</div>'
    )


def _direction_style(direction: str, level_key: str, locked: bool = False) -> tuple:
    if locked:
        return "🔥", "#fdecea", UP_COLOR
    if direction == "buy":
        return {
            "STRONG": ("🔴", "#fdecea", UP_COLOR),
            "MID": ("🟠", "#fef5e7", WARN_COLOR),
            "WEAK": ("🟡", "#fef9e7", "#f39c12"),
            "NOTICE": ("🔵", "#eaf4fb", INFO_COLOR),
            "NEUTRAL": ("⚪", "#f8f9fa", NEUTRAL_COLOR),
        }.get(level_key, ("⚪", "#f8f9fa", NEUTRAL_COLOR))
    return {
        "STRONG": ("🟢", "#eafaf1", DOWN_COLOR),
        "MID": ("🟣", "#f4ecf7", "#8e44ad"),
        "WEAK": ("🟡", "#f8f9fa", "#7f8c8d"),
        "NOTICE": ("⚪", "#f8f9fa", NEUTRAL_COLOR),
        "NEUTRAL": ("⚪", "#f8f9fa", NEUTRAL_COLOR),
    }.get(level_key, ("⚪", "#f8f9fa", NEUTRAL_COLOR))


def format_market_value(value: float, unit: str = "張") -> str:
    if value > 0:
        return f'<span style="color:{UP_COLOR};font-weight:bold;">買超 {value:.0f}{unit}</span>'
    if value < 0:
        return f'<span style="color:{DOWN_COLOR};font-weight:bold;">賣超 {abs(value):.0f}{unit}</span>'
    return f'平盤 0{unit}'


def format_market_value_text(value: float, unit: str = "張") -> str:
    if value > 0:
        return f"買超 {value:.0f}{unit}"
    if value < 0:
        return f"賣超 {abs(value):.0f}{unit}"
    return f"平盤 0{unit}"


def format_twd_billion(value: float | None) -> str:
    if value is None:
        return "-"
    action = "買超" if value >= 0 else "賣超"
    return f"{action} {abs(value) / 100000000:.1f} 億元台幣"


def format_twd_billion_short(value: float | None) -> str:
    if value is None:
        return "-"
    action = "買超" if value >= 0 else "賣超"
    return f"{action}{abs(value) / 100000000:.1f}億"


def volume_ratio_note(value: float | None) -> str:
    if value is None:
        return "量能資料不足。"
    if value >= 1.3:
        return "週日均量明顯高於20日均量；上漲放量偏多，下跌放量偏風險。"
    if value >= 1.05:
        return "週日均量略高於20日均量；代表本週交易熱度較平常增加。"
    if value >= 0.85:
        return "週日均量接近20日均量；量能屬正常範圍。"
    return "週日均量低於20日均量；通常代表觀望或量縮整理。"


def format_ratio_value(value: float) -> str:
    if value > 0:
        return f'<span style="color:{UP_COLOR};font-weight:bold;">+{value:.2f}%</span>'
    if value < 0:
        return f'<span style="color:{DOWN_COLOR};font-weight:bold;">{value:.2f}%</span>'
    return "0.00%"


# ── 評估訊號 ────────────────────────────────────────────────
def evaluate_weighted(df: pd.DataFrame, scfg: dict, inst: dict | None = None,
                      macro: dict | None = None, inst_week: dict | None = None) -> dict:
    thr = scfg["thresholds"]
    ma = scfg["ma_periods"]
    use_obv = scfg.get("use_obv", True)
    use_vol = scfg.get("use_vol_trend", True)
    use_inst = scfg.get("use_institutional", True)
    use_fx = scfg.get("use_fx", True)
    use_rates = scfg.get("use_rates", True)
    macro_sensitivity = scfg.get("macro_sensitivity", "market")
    lev_warn = scfg.get("leverage_warning", False)
    s, m, l = ma["short"], ma["mid"], ma["long"]

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest["Close"])
    prev_close = float(prev["Close"])
    ma_s = float(latest[f"MA{s}"])
    ma_m = float(latest[f"MA{m}"])
    ma_l = float(latest[f"MA{l}"])
    ma_s_prev = float(prev[f"MA{s}"])
    ma_m_prev = float(prev[f"MA{m}"])
    ma_l_prev = float(prev[f"MA{l}"])
    k, d = float(latest["K"]), float(latest["D"])
    kp, dp = float(prev["K"]), float(prev["D"])
    hist = float(latest["MACD_hist"])
    hist_p = float(prev["MACD_hist"])
    bias20 = float(latest["Bias20"])
    vol = float(latest["Volume"])
    vol_ma = float(latest["Vol_MA"])
    vol_trend = float(latest["Vol_Trend"])
    obv = float(latest["OBV"])
    obv_ma = float(latest["OBV_MA"])
    obv_prev = float(prev["OBV"])

    items = []
    buy_score = 0.0
    sell_score = 0.0
    max_possible = float(sum(WEIGHTS.values()) + WEIGHTS["trend"] * 0.35)
    fx_signal = "neutral"
    rate_signal = "neutral"
    inst_signal = "neutral"
    kd_signal = "neutral"
    volume_signal = "neutral"
    obv_signal = "neutral"

    def add_item(label, value, color, note, buy=0.0, sell=0.0):
        nonlocal buy_score, sell_score
        buy_score += buy
        sell_score += sell
        if buy or sell:
            note = f"{note}｜分數影響:正向條件+{buy:.0f}/風險條件+{sell:.0f}"
        items.append((label, value, color, note))

    if lev_warn:
        add_item("⚠️ 槓桿警示", "每日重置ETF，不適合長期持有", "#e67e22",
                 "槓桿ETF有長期耗損效應，短期波動與風險較高")

    b60 = eval_bias60(df, scfg)
    add_item("BIAS60 Z-Score", b60["label"], b60["color"],
             b60["note"] + "｜用途:判斷中期位置是否過熱或超跌；過熱時追價風險偏高")

    ma_s_dir = ma_s > ma_s_prev
    above_ma_s = close > ma_s
    if ma_m > ma_l and above_ma_s and ma_s_dir:
        trend = "healthy_bull"
        trend_label, trend_color = "多頭健康", DOWN_COLOR
        trend_buy, trend_sell = WEIGHTS["trend"], 0
    elif ma_m > ma_l and (not above_ma_s or not ma_s_dir):
        trend = "weak_bull"
        trend_label, trend_color = "多頭轉弱", "#f39c12"
        trend_buy, trend_sell = 0, WEIGHTS["trend"] * 0.4
    elif ma_m < ma_l:
        trend = "bear"
        trend_label, trend_color = "空頭確認", UP_COLOR
        trend_buy, trend_sell = 0, WEIGHTS["trend"]
    else:
        trend = "neutral"
        trend_label, trend_color = "方向不明", NEUTRAL_COLOR
        trend_buy = trend_sell = 0
    add_item(
        "趨勢環境", trend_label, trend_color,
        f"MA{s}={ma_s:.1f}｜MA{m}={ma_m:.1f}｜MA{l}={ma_l:.1f}｜"
        f"收盤{'站上' if above_ma_s else '跌破'}{s}日線（{s}日線{'向上' if ma_s_dir else '向下'}）｜"
        f"趨勢代表目前市場主方向，是本模型最重要的判斷項目｜均線交叉已包含在趨勢判斷中，不重複加分",
        trend_buy, trend_sell,
    )
    regime = classify_market_regime(close, ma_s, ma_m, ma_l, ma_s_prev, ma_m_prev, ma_l_prev)
    ma60_dist = (close / ma_l - 1) * 100 if ma_l else 0.0
    recent_low_ma60_dist = None
    if f"MA{l}" in df.columns:
        ma60_base = df[f"MA{l}"].tail(5).replace(0, pd.NA)
        low_dist = ((df["Low"].tail(5) / ma60_base) - 1) * 100
        if not low_dist.dropna().empty:
            recent_low_ma60_dist = float(low_dist.dropna().min())
    near_ma60_rebound = close > prev_close and (abs(ma60_dist) <= 5 or (recent_low_ma60_dist is not None and recent_low_ma60_dist <= 2))
    if near_ma60_rebound:
        support_note = (
            f"收盤距季線{ma60_dist:+.1f}%｜近5日低點距季線"
            f"{recent_low_ma60_dist:+.1f}%｜偏向支撐反彈，需後續量能與法人延續才算轉強"
        )
        add_item("季線支撐位置", "季線附近支撐反彈", INFO_COLOR, support_note, WEIGHTS["trend"] * 0.20, 0)
    elif close < ma_l and ma_m < ma_l:
        support_note = f"收盤距季線{ma60_dist:+.1f}%｜價格與中期均線同步落在季線下方，反彈先保守看待"
        add_item("季線支撐位置", "跌破季線且中期轉弱", DOWN_COLOR, support_note, 0, WEIGHTS["trend"] * 0.35)
    elif ma60_dist >= 20:
        support_note = f"收盤距季線{ma60_dist:+.1f}%｜中期乖離偏高，安全邊際下降，接近追價風險區"
        add_item("季線支撐位置", "離季線偏遠", WARN_COLOR, support_note, 0, WEIGHTS["trend"] * 0.25)
    else:
        support_note = f"收盤距季線{ma60_dist:+.1f}%｜位置中性，仍以趨勢與籌碼是否延續為主"
        add_item("季線支撐位置", "安全邊際中性", NEUTRAL_COLOR, support_note)

    if use_fx:
        fx = macro.get("fx") if macro else None
        if fx:
            fx_5d = fx.get("chg_5d_pct")
            fx_20d = fx.get("chg_20d_pct")
            fx_value = fx["value"]
            fx_note = (
                f"美元/台幣={fx_value:.3f}｜5日變動={fx_5d:+.2f}%｜20日變動={fx_20d:+.2f}%｜"
                "數字變高代表美元變貴、台幣轉弱；台幣快速貶值常伴隨外資撤出壓力，"
                "但對台積電、聯發科等出口股有部分匯兌抵銷"
            )
            exporter = macro_sensitivity == "exporter"
            full = WEIGHTS["fx"] * (0.75 if exporter else 1.0)
            half = full * 0.5
            if fx_5d is not None and fx_20d is not None and (fx_5d >= 1.0 or fx_20d >= 2.0):
                fx_signal = "risk"
                add_item("美元/台幣匯率", "台幣明顯轉弱 ⚠️", "#e67e22", fx_note, 0, full)
            elif fx_5d is not None and fx_20d is not None and (fx_5d <= -1.0 or fx_20d <= -2.0):
                fx_signal = "support"
                add_item("美元/台幣匯率", "台幣明顯轉強 ✅", UP_COLOR, fx_note, full, 0)
            elif fx_5d is not None and fx_20d is not None and (fx_5d >= 0.5 or fx_20d >= 1.0):
                fx_signal = "mild_risk"
                add_item("美元/台幣匯率", "台幣偏弱", "#f39c12", fx_note, 0, half)
            elif fx_5d is not None and fx_20d is not None and (fx_5d <= -0.5 or fx_20d <= -1.0):
                fx_signal = "mild_support"
                add_item("美元/台幣匯率", "台幣偏強", "#3498db", fx_note, half, 0)
            else:
                add_item("美元/台幣匯率", "匯率中性", NEUTRAL_COLOR, fx_note)
        else:
            reason = "；".join(macro.get("errors", [])) if macro else "未取得總體資料"
            add_item("美元/台幣匯率", "資料暫不可用", "#bdc3c7",
                     f"{reason}｜不計分，避免資料源異常影響判斷")
    else:
        add_item("美元/台幣匯率", "已關閉", "#bdc3c7", "此標的不使用匯率權重")

    if use_rates:
        rates = macro.get("rates") if macro else None
        if rates:
            rate_value = rates["value"]
            bp_5d = rates.get("chg_5d_bp")
            bp_20d = rates.get("chg_20d_bp")
            rate_note = (
                f"美國10年期殖利率={rate_value:.2f}%｜5日變動={bp_5d:+.0f}bp｜20日變動={bp_20d:+.0f}bp｜"
                "殖利率上升會提高股市折現率，通常壓抑科技股評價；殖利率下行則有利成長股估值修復"
            )
            if bp_5d is not None and bp_20d is not None and (bp_5d >= 10 or bp_20d >= 20):
                rate_signal = "risk"
                add_item("利率環境", "殖利率快速上升 ⚠️", DOWN_COLOR, rate_note, 0, WEIGHTS["rates"])
            elif bp_5d is not None and bp_20d is not None and (bp_5d <= -10 or bp_20d <= -20):
                rate_signal = "support"
                add_item("利率環境", "殖利率明顯下行 ✅", UP_COLOR, rate_note, WEIGHTS["rates"], 0)
            elif bp_5d is not None and bp_20d is not None and (bp_5d >= 5 or bp_20d >= 10):
                rate_signal = "mild_risk"
                add_item("利率環境", "利率偏上行", "#f39c12", rate_note, 0, WEIGHTS["rates"] * 0.5)
            elif bp_5d is not None and bp_20d is not None and (bp_5d <= -5 or bp_20d <= -10):
                rate_signal = "mild_support"
                add_item("利率環境", "利率偏下行", "#3498db", rate_note, WEIGHTS["rates"] * 0.5, 0)
            else:
                add_item("利率環境", "利率中性", NEUTRAL_COLOR, rate_note)
        else:
            reason = "；".join(macro.get("errors", [])) if macro else "未取得總體資料"
            add_item("利率環境", "資料暫不可用", "#bdc3c7",
                     f"{reason}｜不計分，避免資料源異常影響判斷")
    else:
        add_item("利率環境", "已關閉", "#bdc3c7", "此標的不使用利率權重")

    hist_series = df["MACD_hist"].dropna()
    hist_p10 = float(hist_series.quantile(0.10))
    hist_p90 = float(hist_series.quantile(0.90))
    macd_note = f"當前={hist:.4f}｜歷史正常區間[{hist_p10:.4f}～{hist_p90:.4f}]｜正=多頭動能，負=空頭動能"
    if hist > 0 and hist_p <= 0:
        add_item("MACD", "柱狀由負翻正 ✅", UP_COLOR, macd_note + "｜剛翻正，動能轉強", WEIGHTS["macd"], 0)
    elif hist < 0 and hist_p >= 0:
        add_item("MACD", "柱狀由正翻負 ⚠️", DOWN_COLOR, macd_note + "｜剛翻負，動能轉弱", 0, WEIGHTS["macd"])
    elif hist > 0 and hist > hist_p:
        add_item("MACD", "多頭動能延續", UP_COLOR, macd_note + "｜動能仍改善", WEIGHTS["macd"] * 0.5, 0)
    elif hist < 0 and hist < hist_p:
        add_item("MACD", "空頭動能延續", DOWN_COLOR, macd_note + "｜動能仍惡化", 0, WEIGHTS["macd"] * 0.5)
    else:
        sign = "正（多頭）" if hist > 0 else "負（空頭）"
        add_item("MACD", f"柱狀持續為{sign}", NEUTRAL_COLOR, macd_note)

    avg_vol20 = float(df["Volume"].tail(20).mean())
    if use_inst:
        if inst and inst.get("success"):
            total_net = float(inst["total_net"])
            net_ratio = total_net / avg_vol20 * 100 if avg_vol20 > 0 else 0.0
            nets = [inst["foreign_net"], inst["invest_net"], inst["dealer_net"]]
            buy_breadth = sum(1 for n in nets if n > 0)
            sell_breadth = sum(1 for n in nets if n < 0)
            inst_note = (
                f"資料日={inst['date']}｜"
                f"外資 {format_market_value(inst['foreign_net']/1000)}｜"
                f"投信 {format_market_value(inst['invest_net']/1000)}｜"
                f"自營 {format_market_value(inst['dealer_net']/1000)}｜"
                f"合計 {format_market_value(total_net/1000)}｜"
                f"占20日均量 {format_ratio_value(net_ratio)}"
            )
            if net_ratio >= 5 and buy_breadth >= 2:
                inst_signal = "strong_buy"
                add_item("三大法人", "法人明顯買超 ✅", UP_COLOR, inst_note, WEIGHTS["institutional"], 0)
            elif net_ratio <= -5 and sell_breadth >= 2:
                inst_signal = "strong_sell"
                add_item("三大法人", "法人明顯賣超 ⚠️", DOWN_COLOR, inst_note, 0, WEIGHTS["institutional"])
            elif net_ratio > 1 or buy_breadth >= 2:
                inst_signal = "buy"
                add_item("三大法人", "法人偏買", UP_COLOR, inst_note, WEIGHTS["institutional"] * 0.5, 0)
            elif net_ratio < -1 or sell_breadth >= 2:
                inst_signal = "sell"
                add_item("三大法人", "法人偏賣", DOWN_COLOR, inst_note, 0, WEIGHTS["institutional"] * 0.5)
            else:
                add_item("三大法人", "籌碼中性", NEUTRAL_COLOR, inst_note)
        else:
            reason = inst.get("error", "未取得資料") if inst else "未取得資料"
            add_item("三大法人", "資料暫不可用", "#bdc3c7",
                     f"{reason}｜不計分，避免資料源異常影響整體判斷")
    else:
        add_item("三大法人", "已關閉（此標的不適用）", "#bdc3c7",
                 "此標的無法直接使用個股三大法人買賣超，避免用錯資料來源")

    kd_buy = k > d and kp <= dp and k < thr["kd_buy"]
    kd_sell = k < d and kp >= dp and k > thr["kd_sell"]
    kd_note = (
        f"當前 K={k:.1f} D={d:.1f}｜低檔正向交叉區:K<{thr['kd_buy']}且K上穿D｜"
        f"高檔風險交叉區:K>{thr['kd_sell']}且K下穿D｜KD適合觀察轉折，但容易鈍化"
    )
    if kd_buy:
        kd_signal = "golden_cross"
        add_item("KD", "低檔黃金交叉 ✅", UP_COLOR, kd_note, WEIGHTS["kd"], 0)
    elif kd_sell:
        kd_signal = "death_cross"
        add_item("KD", "高檔死亡交叉 ⚠️", DOWN_COLOR, kd_note, 0, WEIGHTS["kd"])
    elif k > d and k < 50:
        kd_signal = "turning_up"
        add_item("KD", "低檔轉強但未交叉", "#3498db", kd_note, WEIGHTS["kd"] * 0.4, 0)
    elif k < d and k > 50:
        kd_signal = "turning_down"
        add_item("KD", "高檔轉弱但未交叉", "#f39c12", kd_note, 0, WEIGHTS["kd"] * 0.4)
    else:
        add_item("KD", "無交叉訊號", NEUTRAL_COLOR, kd_note)

    ma_bull = ma_m > ma_l and ma_m_prev <= ma_l_prev
    ma_bear = ma_m < ma_l and ma_m_prev >= ma_l_prev
    ma_note = (
        f"MA{s}={ma_s:.1f}｜MA{m}={ma_m:.1f}｜MA{l}={ma_l:.1f}｜"
        f"這項只說明均線是否剛轉向；分數已在趨勢環境反映，不另外加分"
    )
    if ma_bull:
        add_item("均線交叉", f"MA{m}上穿MA{l} ✅", UP_COLOR, ma_note)
    elif ma_bear:
        add_item("均線交叉", f"MA{m}下穿MA{l} ⚠️", DOWN_COLOR, ma_note)
    else:
        status = "多頭排列持續" if ma_m > ma_l else "空頭排列持續"
        add_item("均線交叉", status, NEUTRAL_COLOR, ma_note)

    vol_ratio = vol / vol_ma if vol_ma > 0 else 1
    if use_vol:
        vol_note = (
            f"最新成交量/{thr['vol_ma_period']}日均量={vol_ratio:.2f}倍｜"
            f"量能是確認項，權重較低"
        )
        if vol_trend > 0 and vol_ratio > 1.2 and close > prev_close:
            volume_signal = "price_up_volume_up"
            add_item("量能趨勢", "價漲量增 ✅", UP_COLOR, vol_note, WEIGHTS["vol"], 0)
        elif vol_trend > 0 and vol_ratio > 1.2 and close < prev_close:
            volume_signal = "price_down_volume_up"
            add_item("量能趨勢", "價跌量增 ⚠️", DOWN_COLOR, vol_note, 0, WEIGHTS["vol"])
        elif vol_trend < 0 and vol_ratio < 0.8 and close < prev_close:
            volume_signal = "price_down_volume_down"
            add_item("量能趨勢", "價跌量縮", "#f39c12", vol_note, 0, WEIGHTS["vol"] * 0.4)
        else:
            add_item("量能趨勢", "量能平穩", NEUTRAL_COLOR, vol_note)
    else:
        add_item("量能趨勢", "已關閉（此標的不適用）", "#bdc3c7",
                 "此標的成交量資料不適合直接作為多空分數")

    if use_obv:
        obv_rising = obv > obv_ma and obv > obv_prev
        obv_falling = obv < obv_ma and obv < obv_prev
        price_up = close > prev_close
        obv_note = (
            f"OBV={'高於' if obv > obv_ma else '低於'}{thr['obv_ma_period']}日均線｜"
            f"OBV可觀察量價累積，但雜訊高於趨勢與MACD"
        )
        if obv_rising and price_up:
            obv_signal = "rising"
            add_item("OBV", "量價齊揚 ✅", UP_COLOR, obv_note, WEIGHTS["obv"], 0)
        elif obv_rising and not price_up:
            obv_signal = "leading"
            add_item("OBV", "OBV領先價格", "#3498db", obv_note, WEIGHTS["obv"] * 0.5, 0)
        elif obv_falling and not price_up:
            obv_signal = "falling"
            add_item("OBV", "量價齊跌 ⚠️", DOWN_COLOR, obv_note, 0, WEIGHTS["obv"])
        elif obv_falling and price_up:
            obv_signal = "divergence"
            add_item("OBV", "價漲量縮背離 ⚠️", "#f39c12", obv_note, 0, WEIGHTS["obv"] * 0.5)
        else:
            add_item("OBV", "OBV中性", NEUTRAL_COLOR, obv_note)
    else:
        add_item("OBV", "已關閉（此標的不適用）", "#bdc3c7",
                 "此標的成交量結構不適合用OBV作為主要判斷")

    is_red = close > float(latest["Open"])
    open_p = float(latest["Open"])
    chg_pct = (close - open_p) / open_p * 100
    price_note = (
        f"開盤={open_p:.2f}｜收盤={close:.2f}｜當日漲跌={chg_pct:+.2f}%｜"
        f"只用來輔助理解今天盤勢，不直接加分"
    )
    add_item("價格行為",
             f"紅K（+{chg_pct:.2f}%）" if is_red else f"黑K（{chg_pct:.2f}%）",
             UP_COLOR if is_red else DOWN_COLOR, price_note)

    effective_buy = 0.0 if b60["locked"] else buy_score
    effective_sell = sell_score
    near_overheated = b60.get("zone") == "overheated" or b60.get("bias60", 0) >= b60.get("p_high", 999) * 0.90 or ma60_dist >= 20
    if near_overheated and not b60["locked"] and effective_buy >= 30:
        effective_buy = min(effective_buy, 29)

    positive_evidence = []
    risk_evidence = []
    if near_ma60_rebound:
        positive_evidence.append("季線附近止跌反彈")
        risk_evidence.append("尚未確認趨勢重新轉強")
    if trend == "healthy_bull":
        positive_evidence.append("主要均線維持多頭")
    elif trend == "weak_bull":
        risk_evidence.append("短線多頭轉弱")
    elif trend == "bear":
        risk_evidence.append("中期趨勢偏空")
    if hist > 0 and hist >= hist_p:
        positive_evidence.append("MACD動能改善")
    elif hist < 0 and hist > hist_p:
        positive_evidence.append("MACD負值收斂")
        risk_evidence.append("MACD仍在空頭區")
    elif hist < hist_p:
        risk_evidence.append("MACD動能轉弱")
    if inst_signal in ("strong_buy", "buy"):
        positive_evidence.append("法人偏買")
    elif inst_signal in ("strong_sell", "sell"):
        risk_evidence.append("法人偏賣")
    if kd_signal in ("golden_cross", "turning_up"):
        positive_evidence.append("KD轉強")
    elif kd_signal in ("death_cross", "turning_down"):
        risk_evidence.append("KD轉弱")
    if volume_signal == "price_up_volume_up":
        positive_evidence.append("價漲量增")
    elif volume_signal == "price_down_volume_up":
        risk_evidence.append("價跌量增")
    if obv_signal in ("rising", "leading"):
        positive_evidence.append("OBV支撐")
    elif obv_signal in ("falling", "divergence"):
        risk_evidence.append("量價背離")
    if fx_signal in ("risk", "mild_risk"):
        risk_evidence.append("台幣轉弱壓抑外資風險偏好")
    elif fx_signal in ("support", "mild_support"):
        positive_evidence.append("台幣轉強支持資金面")
    if rate_signal in ("risk", "mild_risk"):
        risk_evidence.append("美債利率上行壓抑科技評價")
    elif rate_signal in ("support", "mild_support"):
        positive_evidence.append("美債利率下行支持估值修復")
    if b60["locked"] or near_overheated:
        risk_evidence.append("中期乖離偏高")
    trade_context = {
        "price_up": close > prev_close,
        "positive_evidence": positive_evidence,
        "risk_evidence": risk_evidence,
        "ma60_dist": ma60_dist,
        "recent_low_ma60_dist": recent_low_ma60_dist,
    }

    if b60["locked"]:
        level_key, level_label = score_to_signal(effective_sell)
        level, emoji = f"OVERHEATED_{level_key}", "🔥"
        if effective_sell >= 15:
            summary = f"過熱鎖定｜風險條件{level_label}({effective_sell:.0f}/{max_possible:.0f}分)"
        else:
            summary = "過熱鎖定｜追價風險偏高"
        advice = (
            f"季線乖離{b60['bias60']:.1f}%超過歷史門檻，"
            f"原始正向條件分數{buy_score:.0f}分僅供參考，實際正向條件分數歸零"
        )
        bg, border = "#fdecea", UP_COLOR
    elif effective_buy >= effective_sell:
        score = effective_buy
        level_key, level_label = score_to_signal(score)
        emoji, bg, border = _direction_style("buy", level_key)
        level = f"BUY_{level_key}"
        prefix = "超跌正向條件" if b60["zone"] == "oversold" and score >= 15 else "正向條件"
        summary = f"{emoji} {prefix}{level_label}({score:.0f}/{max_possible:.0f}分)"
        advice = {
            "STRONG": "多項高權重指標共振，正向條件較完整",
            "MID": "正向條件有一定一致性，仍需觀察後續確認",
            "WEAK": "值得關注，但仍需等待更多確認",
            "NOTICE": "正向條件初步增加，僅列入觀察",
            "NEUTRAL": "正向條件不足，繼續觀察",
        }[level_key]
    else:
        score = effective_sell
        level_key, level_label = score_to_signal(score)
        emoji, bg, border = _direction_style("sell", level_key)
        level = f"SELL_{level_key}"
        summary = f"{emoji} 風險條件{level_label}({score:.0f}/{max_possible:.0f}分)"
        advice = {
            "STRONG": "多項高權重風險指標共振，風險條件較完整",
            "MID": "風險條件有一定一致性，需提高警覺",
            "WEAK": "風險升溫，追價風險增加",
            "NOTICE": "風險條件初步增加，僅列入觀察",
            "NEUTRAL": "風險條件不足，繼續觀察",
        }[level_key]

    trade_plan = build_trade_plan(level, regime, b60, lev_warn, trade_context)
    pyramid = calc_pyramid(df, scfg, level)
    weekly = build_weekly_metrics(df, scfg, inst_week, regime, b60, effective_buy, effective_sell)

    add_item(
        "本週變化",
        f"{pct_text(weekly['week_chg_pct'])}｜高{weekly['week_high']:.2f} / 低{weekly['week_low']:.2f}",
        UP_COLOR if (weekly["week_chg_pct"] or 0) >= 0 else DOWN_COLOR,
        f"週一開盤={weekly['week_start_open']:.2f}｜週五收盤={close:.2f}｜週一開盤至週五收盤={pct_text(weekly['week_chg_pct'])}｜相對上週五收盤={pct_text(weekly.get('prev_close_chg_pct'))}"
    )
    add_item(
        "週成交量",
        f"{weekly['week_volume'] / 1000:.0f}千股｜均量比{weekly['volume_ratio']:.2f}x" if weekly["volume_ratio"] else f"{weekly['week_volume'] / 1000:.0f}千股",
        UP_COLOR if (weekly["volume_ratio"] or 0) >= 1.15 else NEUTRAL_COLOR,
        f"本週日均量={weekly['week_avg_volume']:.0f}｜20日均量={weekly['avg_volume_20']:.0f}｜量能放大代表趨勢確認度提高"
    )
    if inst_week and inst_week.get("success"):
        inst_color = UP_COLOR if inst_week["total_net"] >= 0 else DOWN_COLOR
        inst_shares = format_market_value(inst_week["total_net"] / 1000)
        inst_amount = format_twd_billion(weekly.get("institutional_value"))
        inst_value = f"{inst_amount}｜{inst_shares}"
        inst_note = f"本週三大法人個股買賣超張數來自證交所 T86；金額為張數乘以收盤價的約略估算｜統計{inst_week.get('days', 0)}個交易日｜{inst_week.get('date_range', '')}"
    else:
        inst_color = NEUTRAL_COLOR
        inst_value = "不適用或未取得"
        inst_note = (inst_week or {}).get("error", "本週法人資料未取得")
    add_item("本週三大法人", inst_value, inst_color, inst_note)
    add_item("均線位置", weekly["ma_position"], weekly["posture_color"], "觀察收盤價相對10/20/60日線的位置，判斷續強、轉弱或盤整")
    add_item("本週趨勢總結", weekly["trend_summary"], weekly["posture_color"], "週報偏向中短線趨勢追蹤，僅供條件觀察")
    add_item("下週觀察", weekly["next_focus"], weekly["posture_color"], "下週以關鍵均線、本週高低點、量能與法人買賣超是否延續作為觀察重點")

    return dict(
        level=level, emoji=emoji, summary=summary, advice=advice,
        bg=bg, border=border, items=items,
        close=close, bias20=bias20, is_red=is_red,
        buy_score=buy_score, sell_score=sell_score,
        effective_buy=effective_buy, effective_sell=effective_sell,
        score_note="季線乖離過熱，正向條件分數已鎖定" if b60["locked"] else "",
        max_possible=max_possible, b60=b60, regime=regime,
        trade_plan=trade_plan, pyramid=pyramid, weekly=weekly,
    )


# ── 產生單檔 HTML 區塊 ───────────────────────────────────────
def stock_html_block(name: str, ticker: str, result: dict, note: str = "") -> str:
    rows = ""
    for idx, (label, value, color, n) in enumerate(result["items"]):
        # 把備註用｜切開，每段變成一個編號子項目
        parts = [p.strip() for p in n.split("｜") if p.strip()]
        note_items = "".join(
            f'<span style="display:block;margin:1px 0;">'
            f'<span style="color:#aaa;margin-right:4px;">{i+1}.</span>{p}</span>'
            for i, p in enumerate(parts)
        )
        bg_row = "#fafafa" if idx % 2 == 0 else "#ffffff"
        rows += (
            f'<tr style="background:{bg_row};border-bottom:1px solid #eee;">'
            f'<td style="padding:10px 12px;color:#555;width:22%;font-size:13px;'
            f'font-weight:bold;vertical-align:top;line-height:1.5;">{label}</td>'
            f'<td style="padding:8px 10px;font-weight:bold;color:{color};'
            f'font-size:13px;vertical-align:top;line-height:1.5;width:25%;">{value}</td>'
            f'<td style="padding:10px 12px;color:#666;font-size:12px;'
            f'line-height:1.6;vertical-align:top;">{note_items}</td>'
            f'</tr>'
        )

    note_html = ""
    if note:
        note_html = (f'<div style="background:#fef9e7;padding:8px 16px;'
                     f'font-size:12px;color:#7d6608;border-bottom:1px solid #eee;">'
                     f'{note}</div>')

    trade_html = (
        f'<div style="background:#fff;padding:12px 16px;border-bottom:1px solid #eee;">'
        f'{trade_plan_html(result)}</div>'
    )

    pyramid_html = ""
    if result["pyramid"]["suggestions"]:
        sugg = "".join(f'<li style="margin:4px 0;font-size:13px;">{s}</li>'
                       for s in result["pyramid"]["suggestions"])
        pyramid_html = (f'<div style="background:#f0f8ff;padding:12px 16px;border-top:1px solid #d6eaf8;">'
                        f'<div style="font-weight:bold;color:#2471a3;margin-bottom:6px;">🏗️ 分層條件觀察</div>'
                        f'<ul style="margin:0;padding-left:18px;">{sugg}</ul></div>')

    return (
        f'<div style="margin-bottom:28px;border:2px solid {result["border"]};'
        f'border-radius:10px;overflow:hidden;background:#fff;">'
        # 標題列
        f'<div style="background:{result["border"]};padding:12px 16px;'
        f'display:flex;justify-content:space-between;align-items:center;">'
        f'<span style="color:#fff;font-size:16px;font-weight:bold;">'
        f'{result["emoji"]} {name} ({ticker.replace(".TW","").replace(".tw","")})</span>'
        f'<span style="color:#fff;font-size:20px;font-weight:bold;">{result["close"]:.2f}</span>'
        f'</div>'
        # 個股備註
        f'{note_html}'
        # 實際交易建議
        f'{trade_html}'
        # 指標明細表格
        f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'
        # 金字塔建議
        f'{pyramid_html}</div>'
    )


# ── 產生總覽表格 ─────────────────────────────────────────────
def summary_table(results: list) -> str:
    cards = ""
    for name, ticker, r in results:
        code = ticker.replace(".TW", "").replace(".tw", "")
        weekly = r.get("weekly", {})
        posture = weekly.get("posture", "觀察")
        posture_color = weekly.get("posture_color", r["border"])
        week_chg_pct = weekly.get("week_chg_pct")
        week_chg = pct_text(week_chg_pct)
        chg_color = UP_COLOR if (week_chg_pct or 0) >= 0 else DOWN_COLOR
        next_focus = html_lib.escape(_social_short_text(weekly.get("next_focus", ""), 110))
        ma_position = html_lib.escape(_social_short_text(weekly.get("ma_position", "-"), 48))
        inst_total = weekly.get("institutional_total")
        inst_text = format_market_value(inst_total / 1000) if inst_total is not None else "-"
        vol_ratio = weekly.get("volume_ratio")
        vol_text = f"{vol_ratio:.2f}x" if vol_ratio is not None else "-"
        high_low = f"{weekly.get('week_high', 0):.2f} / {weekly.get('week_low', 0):.2f}"
        cards += (
            f'<div style="border:1px solid #ddd;border-left:5px solid {posture_color};'
            f'border-radius:8px;padding:14px 16px;margin-bottom:14px;background:#fff;">'
            f'<div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;">'
            f'<div style="min-width:0;">'
            f'<div style="font-size:16px;font-weight:bold;color:#2c3e50;line-height:1.4;">{name}</div>'
            f'<div style="font-size:12px;color:#888;margin-top:2px;">代號 {code}</div>'
            f'</div>'
            f'<div style="text-align:right;white-space:nowrap;">'
            f'<div style="font-size:11px;color:#888;">收盤 / 本週</div>'
            f'<div style="font-size:18px;font-weight:bold;color:#2c3e50;">{r["close"]:.2f}</div>'
            f'<div style="font-size:12px;font-weight:bold;color:{chg_color};">{week_chg}</div>'
            f'</div></div>'
            f'<div style="margin-top:8px;color:{posture_color};font-size:15px;font-weight:bold;">{posture}</div>'
            f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:9px;">'
            f'<div style="background:#f7f9fb;border-radius:6px;padding:7px 8px;">'
            f'<div style="font-size:11px;color:#888;">本週高 / 低</div>'
            f'<div style="font-size:12px;font-weight:bold;color:#2c3e50;">{high_low}</div></div>'
            f'<div style="background:#f7f9fb;border-radius:6px;padding:7px 8px;">'
            f'<div style="font-size:11px;color:#888;">法人週合計</div>'
            f'<div style="font-size:12px;font-weight:bold;color:#2c3e50;">{inst_text}</div></div>'
            f'<div style="background:#f7f9fb;border-radius:6px;padding:7px 8px;">'
            f'<div style="font-size:11px;color:#888;">量能比</div>'
            f'<div style="font-size:12px;font-weight:bold;color:#2c3e50;">{vol_text}</div></div>'
            f'<div style="background:#f7f9fb;border-radius:6px;padding:7px 8px;">'
            f'<div style="font-size:11px;color:#888;">均線位置</div>'
            f'<div style="font-size:12px;font-weight:bold;color:#2c3e50;">{ma_position}</div></div>'
            f'</div>'
            f'<div style="margin-top:8px;color:#666;font-size:12px;line-height:1.55;">下週觀察：{next_focus}</div>'
            f'</div>'
        )
    return f'<div style="margin-bottom:28px;">{cards}</div>'


def market_context_html(macro: dict | None) -> str:
    if not macro:
        return ""

    fx = macro.get("fx")
    rates = macro.get("rates")
    fx_html = ""
    rates_html = ""

    if fx:
        fx_html = (
            f'<div style="padding:10px 12px;border-bottom:1px solid #eee;">'
            f'<strong>美元/台幣</strong>：{fx["value"]:.3f}｜'
            f'5日 {fx["chg_5d_pct"]:+.2f}%｜20日 {fx["chg_20d_pct"]:+.2f}%'
            f'<div style="color:#777;font-size:12px;margin-top:3px;">'
            f'數字變高代表台幣轉弱；短線通常提高外資撤出與台股修正風險，但出口股有部分匯兌抵銷。</div></div>'
        )

    if rates:
        rates_html = (
            f'<div style="padding:10px 12px;">'
            f'<strong>美國10年期公債殖利率</strong>：{rates["value"]:.2f}%｜'
            f'5日 {rates["chg_5d_bp"]:+.0f}bp｜20日 {rates["chg_20d_bp"]:+.0f}bp'
            f'<div style="color:#777;font-size:12px;margin-top:3px;">'
            f'殖利率上升通常壓抑科技股評價；殖利率下行則有利成長股估值修復。</div></div>'
        )

    if not fx_html and not rates_html:
        errors = "；".join(macro.get("errors", [])) or "未取得總體資料"
        return (f'<div style="background:#fff3cd;border:1px solid #ffeeba;'
                f'padding:10px 12px;border-radius:6px;margin-bottom:18px;'
                f'font-size:12px;color:#856404;">總體資料暫不可用：{errors}</div>')

    return (
        f'<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">總體環境</h3>'
        f'<div style="border:1px solid #ddd;border-radius:8px;overflow:hidden;margin-bottom:28px;">'
        f'{fx_html}{rates_html}</div>'
    )


def _classify_news_item(title: str) -> tuple:
    text = title.lower()
    high_keywords = ["戰爭", "開戰", "伊朗", "美伊", "霍爾木茲", "關稅", "晶片管制", "fomc", "fed", "川習", "習近平", "trump", "xi"]
    mid_keywords = ["原油", "油價", "利率", "殖利率", "匯率", "台積電", "tsmc", "nvidia", "ai", "半導體", "外資", "營收", "法說"]

    if any(k in text for k in high_keywords):
        impact = "高"
    elif any(k in text for k in mid_keywords):
        impact = "中高"
    else:
        impact = "中"

    if any(k in text for k in ["原油", "油價", "中東", "伊朗", "美伊", "霍爾木茲"]):
        note = "能源與地緣風險會影響通膨、利率預期與科技股評價；油價急漲通常壓抑風險偏好。"
        scope = "油價、通膨、全球股市、台股風險偏好"
    elif any(k in text for k in ["fed", "fomc", "利率", "殖利率"]):
        note = "利率預期會直接影響成長股估值；偏鷹訊息通常壓抑半導體與高本益比族群。"
        scope = "全球股市、美元、科技股、外資資金流"
    elif any(k in text for k in ["川習", "美中", "關稅", "晶片管制", "trump", "xi"]):
        note = "美中談判與晶片政策會影響半導體供應鏈、外資風險偏好與台股權值股評價。"
        scope = "台股、半導體、匯率、外資風險偏好"
    elif any(k in text for k in ["台積電", "tsmc", "nvidia", "ai", "半導體", "營收", "法說"]):
        note = "AI與半導體需求變化會影響台積電、聯發科與加權指數權值股表現。"
        scope = "台積電、聯發科、半導體供應鏈"
    else:
        note = "屬於市場風險偏好觀察項，需搭配價格、籌碼與總體環境判斷。"
        scope = "台股與全球風險偏好"
    return impact, scope, note


def _is_market_relevant_news(title: str) -> bool:
    text = title.lower()
    keywords = [
        "台股", "加權", "櫃買", "外資", "匯率", "台幣", "半導體", "晶片", "關稅",
        "美中", "川習", "習近平", "trump", "xi", "fed", "fomc", "利率", "殖利率",
        "原油", "油價", "中東", "伊朗", "美伊", "霍爾木茲", "台積電", "tsmc",
        "聯發科", "台達電", "鴻海", "廣達", "緯創", "緯穎", "nvidia", "ai",
        "ai伺服器", "cnyes", "鉅亨",
    ]
    return any(keyword in text for keyword in keywords)



def _is_market_event_candidate(title: str) -> bool:
    text = title.lower()
    market_terms = [
        "台股", "加權", "外資", "匯率", "台幣", "半導體", "晶片", "台積電", "tsmc",
        "nvidia", "ai", "聯發科", "鴻海", "台達電", "廣達", "緯創", "緯穎",
        "fed", "fomc", "cpi", "pce", "利率", "美中", "關稅", "晶片管制",
        "原油", "opec", "中東", "戰爭", "財報", "營收", "法說"
    ]
    event_terms = [
        "重大事件", "行事曆", "本週", "下週", "下周", "未來", "一個月", "法說", "法說會",
        "營收", "財報", "公告", "決議", "會議", "公布", "發布", "財測", "展望",
        "股東會", "除息", "fomc", "cpi", "pce", "opec", "管制", "關稅"
    ]
    return any(k in text for k in market_terms) and any(k in text for k in event_terms)


def _infer_event_date_from_title(title: str, report_date) -> object | None:
    text = str(title or "")
    patterns = [
        r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?",
        r"(\d{1,2})[/-](\d{1,2})",
        r"(\d{1,2})月(\d{1,2})日?",
    ]
    for idx, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            if idx == 0:
                year, month, day = map(int, match.groups())
            else:
                year = report_date.year
                month, day = map(int, match.groups())
                candidate = datetime(year, month, day).date()
                if candidate < report_date - timedelta(days=60):
                    year += 1
            return datetime(year, month, day).date()
        except ValueError:
            continue

    relative_map = {
        "今天": 0,
        "今日": 0,
        "明天": 1,
        "後天": 2,
        "本週": 0,
        "這週": 0,
        "下週": 7,
        "下周": 7,
        "下個月": 30,
    }
    for keyword, offset in relative_map.items():
        if keyword in text:
            return report_date + timedelta(days=offset)
    return None


def fetch_auto_market_events(cfg: dict, report_date: str) -> list:
    event_cfg = cfg.get("auto_market_events", {})
    if not event_cfg.get("enabled", True):
        return []

    report_dt = datetime.strptime(report_date, "%Y-%m-%d").date()
    lookback_days = int(event_cfg.get("lookback_days", cfg.get("market_events_lookback_days", 5)))
    lookahead_days = int(event_cfg.get("lookahead_days", cfg.get("market_events_lookahead_days", 30)))
    start = report_dt - timedelta(days=lookback_days)
    end = report_dt + timedelta(days=lookahead_days)
    queries = event_cfg.get("queries", [])
    max_items = int(event_cfg.get("max_items", 8))
    max_items_per_query = int(event_cfg.get("max_items_per_query", 2))
    headers = {"User-Agent": "Mozilla/5.0"}
    items = []
    seen = set()

    for query in queries:
        url = (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query + f' when:{max(lookback_days, 7)}d')}"
            "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception:
            continue

        query_count = 0
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_text = item.findtext("pubDate", "").strip()
            source = item.findtext("source", "").strip() or "Google News"
            if not title or not _is_market_event_candidate(title):
                continue
            try:
                pub_dt = parsedate_to_datetime(pub_text).astimezone(TAIPEI_TZ)
            except Exception:
                pub_dt = datetime.combine(report_dt, datetime.min.time()).replace(tzinfo=TAIPEI_TZ)
            if pub_dt.date() < start:
                continue
            inferred_date = _infer_event_date_from_title(title, report_dt)
            event_dt = inferred_date or pub_dt.date()
            if not (start <= event_dt <= end):
                continue
            key = re.sub(r"\s+", "", f"{event_dt.isoformat()}{title}{source}".lower())
            if key in seen:
                continue
            impact, scope, note = _classify_news_item(title)
            if inferred_date is None:
                note = f"{note}（未偵測到明確日期，暫以新聞發布日作為觀察日。）"
            seen.add(key)
            items.append({
                "date": event_dt.strftime("%Y-%m-%d"),
                "_event_date": event_dt,
                "_published_at": pub_dt,
                "title": title,
                "impact": impact,
                "scope": scope,
                "note": note,
                "source": source,
                "link": link,
            })
            query_count += 1
            if query_count >= max_items_per_query:
                break

    if event_cfg.get("fallback_manual_events", False) and not items:
        for event in cfg.get("market_events", []):
            try:
                event_dt = datetime.strptime(event.get("date", ""), "%Y-%m-%d").date()
            except Exception:
                continue
            if start <= event_dt <= end:
                item = dict(event)
                item["_event_date"] = event_dt
                item.setdefault("source", "config.json market_events 備援")
                items.append(item)

    impact_rank = {"高": 4, "中高": 3, "中": 2, "低": 1}
    items.sort(key=lambda x: (x.get("_event_date", report_dt), -impact_rank.get(x.get("impact", ""), 0)))
    for item in items:
        item.pop("_event_date", None)
        item.pop("_published_at", None)
    return items[:max_items]
def fetch_auto_news(cfg: dict) -> list:
    news_cfg = cfg.get("auto_news", {})
    if not news_cfg.get("enabled", False):
        return []

    queries = news_cfg.get("queries", [])
    lookback_days = int(news_cfg.get("lookback_days", 3))
    max_items = int(news_cfg.get("max_items", 8))
    max_items_per_query = int(news_cfg.get("max_items_per_query", 3))
    now = datetime.now(TAIPEI_TZ)
    min_date = now - timedelta(days=lookback_days)
    headers = {"User-Agent": "Mozilla/5.0"}
    items = []
    seen = set()

    for query in queries:
        url = (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query + f' when:{lookback_days}d')}"
            "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception:
            continue

        query_count = 0
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_text = item.findtext("pubDate", "").strip()
            source = item.findtext("source", "").strip() or "Google News"
            if not title:
                continue
            if not _is_market_relevant_news(title):
                continue
            try:
                pub_dt = parsedate_to_datetime(pub_text).astimezone(TAIPEI_TZ)
            except Exception:
                pub_dt = now
            if pub_dt < min_date:
                continue
            key = re.sub(r"\s+", "", f"{title}{source}".lower())
            if key in seen:
                continue
            impact, scope, note = _classify_news_item(title)
            seen.add(key)
            items.append({
                "date": pub_dt.strftime("%Y-%m-%d %H:%M"),
                "_published_at": pub_dt,
                "title": title,
                "impact": impact,
                "scope": scope,
                "note": note,
                "source": source,
                "link": link,
            })
            query_count += 1
            if query_count >= max_items_per_query:
                break

    impact_rank = {"高": 3, "中高": 2, "中": 1, "低": 0}
    items.sort(key=lambda x: (x["_published_at"], impact_rank.get(x["impact"], 0)), reverse=True)
    for item in items:
        item.pop("_published_at", None)
    return items[:max_items]


def market_events_html(cfg: dict, today: str, event_items: list | None = None,
                       news_items: list | None = None) -> str:
    event_cfg = cfg.get("auto_market_events", {})
    lookback_days = int(event_cfg.get("lookback_days", cfg.get("market_events_lookback_days", 5)))
    lookahead_days = int(event_cfg.get("lookahead_days", cfg.get("market_events_lookahead_days", 30)))
    event_rows = ""
    news_rows = ""
    impact_colors = {"高": "#c0392b", "中高": "#e67e22", "中": "#f39c12", "低": "#7f8c8d"}

    for event in event_items or []:
        color = impact_colors.get(event.get("impact", ""), "#7f8c8d")
        title = html_lib.escape(event.get("title", ""))
        source = html_lib.escape(event.get("source", "Google News"))
        link = html_lib.escape(event.get("link", ""))
        linked_title = f'<a href="{link}" style="color:#2c3e50;text-decoration:none;">{title}</a>' if link else title
        event_rows += (
            f'<tr style="border-bottom:1px solid #eee;">'
            f'<td style="padding:9px 12px;white-space:nowrap;color:#555;">{html_lib.escape(event.get("date", ""))}</td>'
            f'<td style="padding:9px 12px;font-weight:bold;">{linked_title}</td>'
            f'<td style="padding:9px 12px;">'
            f'<span style="background:{color};color:#fff;font-size:11px;padding:2px 7px;border-radius:4px;white-space:nowrap;display:inline-block;">'
            f'{html_lib.escape(event.get("impact", "未評估"))}</span></td>'
            f'<td style="padding:9px 12px;color:#666;font-size:12px;line-height:1.6;">'
            f'{html_lib.escape(event.get("scope", ""))}｜{html_lib.escape(event.get("note", ""))}'
            f'<div style="color:#aaa;margin-top:3px;">來源：{source}</div></td>'
            f'</tr>'
        )

    if not event_rows:
        event_rows = (f'<tr><td style="padding:10px 12px;color:#777;font-size:12px;line-height:1.6;" colspan="4">'
                      f'過去 {lookback_days} 天至未來 {lookahead_days} 天內尚未掃描到高關聯重大事件。</td></tr>')

    for item in news_items or []:
        color = impact_colors.get(item.get("impact", ""), "#7f8c8d")
        title = html_lib.escape(item.get("title", ""))
        source = html_lib.escape(item.get("source", "Google News"))
        link = html_lib.escape(item.get("link", ""))
        linked_title = f'<a href="{link}" style="color:#2c3e50;text-decoration:none;">{title}</a>' if link else title
        news_rows += (
            f'<tr style="border-bottom:1px solid #eee;">'
            f'<td style="padding:9px 12px;white-space:nowrap;color:#555;">{html_lib.escape(item.get("date", ""))}</td>'
            f'<td style="padding:9px 12px;font-weight:bold;">{linked_title}</td>'
            f'<td style="padding:9px 12px;">'
            f'<span style="background:{color};color:#fff;font-size:11px;padding:2px 7px;border-radius:4px;white-space:nowrap;display:inline-block;">'
            f'{html_lib.escape(item.get("impact", "未評估"))}</span></td>'
            f'<td style="padding:9px 12px;color:#666;font-size:12px;line-height:1.6;">'
            f'{html_lib.escape(item.get("scope", ""))}｜{html_lib.escape(item.get("note", ""))}'
            f'<div style="color:#aaa;margin-top:3px;">來源：{source}</div></td>'
            f'</tr>'
        )

    if not news_rows:
        news_rows = (f'<tr><td style="padding:10px 12px;color:#777;font-size:12px;" colspan="4">'
                     f'近 {cfg.get("auto_news", {}).get("lookback_days", 7)} 天未抓到符合條件的高關聯新聞。</td></tr>')

    return (
        f'<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">消息面與重大事件</h3>'
        f'<div style="font-size:12px;color:#777;margin:-12px 0 10px;line-height:1.6;">'
        f'自動重大事件掃描顯示報告日前 {lookback_days} 天至後 {lookahead_days} 天內，偏向行事曆、法說會、財報、Fed、CPI、PCE、關稅、晶片管制、AI半導體、原油與匯率等對台股有影響的事件；近期新聞掃描保留原本近 '
        f'{cfg.get("auto_news", {}).get("lookback_days", 3)} 天高關聯消息。</div>'
        f'<div style="font-weight:bold;color:#2c3e50;margin:4px 0 6px;">自動重大事件掃描</div>'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:28px;'
        f'border:1px solid #ddd;border-radius:8px;overflow:hidden;">'
        f'<thead><tr style="background:#34495e;color:#fff;">'
        f'<th style="padding:10px 12px;text-align:left;">日期</th>'
        f'<th style="padding:10px 12px;text-align:left;">事件</th>'
        f'<th style="padding:10px 12px;text-align:left;">影響</th>'
        f'<th style="padding:10px 12px;text-align:left;">可能影響</th>'
        f'</tr></thead><tbody>{event_rows}</tbody></table>'
        f'<div style="font-weight:bold;color:#2c3e50;margin:4px 0 6px;">近期自動新聞掃描</div>'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:28px;'
        f'border:1px solid #ddd;border-radius:8px;overflow:hidden;">'
        f'<thead><tr style="background:#566573;color:#fff;">'
        f'<th style="padding:10px 12px;text-align:left;">日期</th>'
        f'<th style="padding:10px 12px;text-align:left;">新聞</th>'
        f'<th style="padding:10px 12px;text-align:left;">影響</th>'
        f'<th style="padding:10px 12px;text-align:left;">可能影響</th>'
        f'</tr></thead><tbody>{news_rows}</tbody></table>'
    )
def scoring_rules_html() -> str:
    weights = [
        ("趨勢方向", WEIGHTS["trend"], "市場主方向"),
        ("MACD動能", WEIGHTS["macd"], "漲跌動能"),
        ("三大法人", WEIGHTS["institutional"], "法人籌碼"),
        ("KD", WEIGHTS["kd"], "條件轉折時機"),
        ("OBV", WEIGHTS["obv"], "量價配合"),
        ("台幣匯率", WEIGHTS["fx"], "台幣強弱影響外資流向與出口股獲利"),
        ("美國利率", WEIGHTS["rates"], "利率升降影響科技股評價"),
        ("量能", WEIGHTS["vol"], "成交確認"),
    ]
    weight_rows = "".join(
        f'<tr style="border-bottom:1px solid #eee;">'
        f'<td style="padding:7px 9px;font-weight:bold;color:#2c3e50;">{name}</td>'
        f'<td style="padding:7px 9px;text-align:right;color:#c0392b;font-weight:bold;">{score}</td>'
        f'<td style="padding:7px 9px;color:#777;font-size:12px;">{meaning}</td>'
        f'</tr>'
        for name, score, meaning in weights
    )
    trade_rows = "".join(
        f'<tr style="border-bottom:1px solid #eee;">'
        f'<td style="padding:7px 9px;font-weight:bold;color:#2c3e50;">{level}</td>'
        f'<td style="padding:7px 9px;color:#777;font-size:12px;">{note}</td>'
        f'</tr>'
        for level, note in [
            ("提醒", "只提醒市場溫度變化，僅供條件觀察"),
            ("弱訊號", "代表少數條件出現，不能單獨當成趨勢依據"),
            ("中訊號", "代表多項條件開始一致，需觀察是否持續"),
            ("強訊號", "代表高權重條件共振，但仍需保留後續調整空間"),
        ]
    )
    return (
        f'<details style="background:#f7fbff;border:1px solid #cfe2f3;border-radius:8px;'
        f'padding:12px 14px;margin-bottom:22px;">'
        f'<summary style="cursor:pointer;font-weight:bold;color:#1f4e79;font-size:15px;">'
        f'評分標準</summary>'
        f'<div style="margin-top:12px;">'
        f'<div style="font-size:13px;color:#555;line-height:1.7;margin-bottom:12px;">'
        f'系統會分別計算正向條件與風險條件分數，最後以「實際參考分」作為主要判斷。'
        f'若季線乖離過熱，正向條件分數會被歸零，只保留背景分數呈現原本有哪些條件偏多。</div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;">'
        f'<span style="background:#eef5fb;border:1px solid #d6eaf8;border-radius:6px;padding:5px 8px;font-size:12px;white-space:nowrap;display:inline-block;">提醒 15-29</span>'
        f'<span style="background:#fef9e7;border:1px solid #f9e79f;border-radius:6px;padding:5px 8px;font-size:12px;white-space:nowrap;display:inline-block;">弱 30-49</span>'
        f'<span style="background:#fef5e7;border:1px solid #fad7a0;border-radius:6px;padding:5px 8px;font-size:12px;white-space:nowrap;display:inline-block;">中 50-69</span>'
        f'<span style="background:#fdecea;border:1px solid #f5b7b1;border-radius:6px;padding:5px 8px;font-size:12px;white-space:nowrap;display:inline-block;">強 70+</span>'
        f'</div>'
        f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5eef7;'
        f'border-radius:6px;overflow:hidden;">'
        f'<thead><tr style="background:#eaf4fb;color:#1f4e79;">'
        f'<th style="padding:8px 9px;text-align:left;">指標</th>'
        f'<th style="padding:8px 9px;text-align:right;">分數</th>'
        f'<th style="padding:8px 9px;text-align:left;">用途</th>'
        f'</tr></thead><tbody>{weight_rows}</tbody></table>'
        f'<div style="font-size:12px;color:#777;line-height:1.6;margin-top:10px;">'
        f'BIAS60 用來判斷中期過熱或超跌，不直接加分；過熱時會暫停計入正向條件分數，避免過度解讀。</div>'
        f'<div style="font-weight:bold;color:#1f4e79;font-size:14px;margin:14px 0 8px;">條件訊號怎麼看</div>'
        f'<div style="font-size:13px;color:#555;line-height:1.7;margin-bottom:10px;">'
        f'這裡說明訊號等級的用途，不代表具體交易動作或部位比例。'
        f'系統會再依市場狀態調整條件權重，以區分趨勢、修正與盤整環境。'
        f'同一等級訊號連續出現時，不代表市場狀態已改變。</div>'
        f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5eef7;'
        f'border-radius:6px;overflow:hidden;">'
        f'<thead><tr style="background:#eaf4fb;color:#1f4e79;">'
        f'<th style="padding:8px 9px;text-align:left;">等級</th>'
        f'<th style="padding:8px 9px;text-align:left;">實際用途</th>'
        f'</tr></thead><tbody>{trade_rows}</tbody></table>'
        f'</div></details>'
    )



# ── 週報版呈現輔助 ───────────────────────────────────────────
def _plain_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _plain_inst_text(value: float | None) -> str:
    if value is None:
        return "-"
    return format_market_value_text(value / 1000)


def _pct_color(value: float | None) -> str:
    return UP_COLOR if (value or 0) >= 0 else DOWN_COLOR


def _escape(value) -> str:
    return html_lib.escape(str(value or ""))


def _svg_polyline(points: list[tuple[float, float]], color: str, width: float = 3.0, dash: str = "") -> str:
    if len(points) < 2:
        return ""
    raw = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    dash_attr = f" stroke-dasharray='{dash}'" if dash else ""
    return f"<polyline points='{raw}' fill='none' stroke='{color}' stroke-width='{width}' stroke-linecap='round' stroke-linejoin='round'{dash_attr}/>"


def render_sparkline(values: list, width: int = 120, height: int = 34, color: str | None = None) -> str:
    clean = []
    for value in values or []:
        try:
            clean.append(float(value))
        except Exception:
            pass
    if len(clean) < 2:
        return f"<div style='height:{height}px;color:#9a927e;font-size:11px;'>資料不足</div>"
    lo, hi = min(clean), max(clean)
    span = hi - lo or 1.0
    pts = []
    for i, value in enumerate(clean):
        x = width * i / max(len(clean) - 1, 1)
        y = height - 4 - ((value - lo) / span * (height - 8))
        pts.append((x, y))
    line_color = color or (_pct_color(clean[-1] - clean[0]))
    return f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' xmlns='http://www.w3.org/2000/svg'><polyline points='{' '.join(f'{x:.1f},{y:.1f}' for x,y in pts)}' fill='none' stroke='{line_color}' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>"


def render_price_chart(result: dict, width: int = 760, height: int = 300, compact: bool = False) -> str:
    weekly = result.get("weekly", {})
    points = weekly.get("chart_points", [])
    if len(points) < 2:
        return "<div style='color:#7a8178;font-size:13px;'>走勢資料不足，暫無線圖。</div>"

    pad_l, pad_r, pad_t, pad_b = 54, 18, 22, 38
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    values = []
    for pt in points:
        for key in ("close", "ma_s", "ma_m", "ma_l"):
            val = pt.get(key)
            if val is not None:
                values.append(float(val))
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0

    def xy(i: int, value: float) -> tuple[float, float]:
        x = pad_l + (plot_w * i / max(len(points) - 1, 1))
        y = pad_t + plot_h - ((value - lo) / span * plot_h)
        return x, y

    def series(key: str, color: str, width_line: float, dash: str = "") -> str:
        pts = []
        for i, pt in enumerate(points):
            val = pt.get(key)
            if val is not None:
                pts.append(xy(i, float(val)))
        return _svg_polyline(pts, color, width_line, dash)

    week_start_idx = max(len(points) - 5, 0)
    week_start_x = pad_l + plot_w * week_start_idx / max(len(points) - 1, 1)
    week_points = [xy(i, float(pt["close"])) for i, pt in enumerate(points) if i >= week_start_idx and pt.get("close") is not None]
    close = points[-1]["close"]
    week_chg = weekly.get("week_chg_pct")
    week_color = _pct_color(week_chg)
    title_size = 18 if compact else 20
    svg = f"""
    <svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="近60日價格走勢">
      <rect x="0" y="0" width="{width}" height="{height}" rx="18" fill="#fffdf7"/>
      <rect x="{week_start_x:.1f}" y="{pad_t}" width="{width - pad_r - week_start_x:.1f}" height="{plot_h}" fill="#efe5bd" opacity="0.32"/>
      <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" stroke="#d8d1bd"/>
      <line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" y2="{pad_t + plot_h}" stroke="#d8d1bd"/>
      {series('ma_l', '#8f9a91', 2.0, '6 5')}
      {series('ma_m', '#c9a227', 2.2)}
      {series('ma_s', '#6d8f7a', 2.0)}
      {series('close', '#aeb7ad', 2.4)}
      {_svg_polyline(week_points, week_color, 6.0)}
      <text x="{pad_l}" y="{height - 13}" fill="#6f776f" font-size="13">近60日收盤價與 MA10 / MA20 / MA60；粗線為本週5日走勢</text>
      <text x="{width - pad_r}" y="{pad_t + 4}" text-anchor="end" fill="#12322b" font-size="{title_size}" font-weight="800">{close:.2f} / {pct_text(week_chg)}</text>
      <text x="{pad_l}" y="18" fill="#6f776f" font-size="12">高 {hi:.2f}</text>
      <text x="{pad_l}" y="{pad_t + plot_h - 6}" fill="#6f776f" font-size="12">低 {lo:.2f}</text>
    </svg>"""
    return svg


def render_week_price_chart(result: dict, width: int = 650, height: int = 280) -> str:
    weekly = result.get("weekly", {})
    points = weekly.get("week_chart_points", [])
    clean = []
    for pt in points:
        try:
            clean.append({"date": str(pt.get("date", "")), "value": float(pt.get("value"))})
        except Exception:
            continue
    if len(clean) < 2:
        return "<div style='color:#7a8178;font-size:13px;'>本週走勢資料不足，暫無線圖。</div>"

    pad_l, pad_r, pad_t, pad_b = 54, 22, 24, 42
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    values = [pt["value"] for pt in clean]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi += 1
        lo -= 1
    margin = (hi - lo) * 0.12
    lo -= margin
    hi += margin
    span = hi - lo or 1.0

    pts = []
    for i, pt in enumerate(clean):
        x = pad_l + plot_w * i / max(len(clean) - 1, 1)
        y = pad_t + plot_h - ((pt["value"] - lo) / span * plot_h)
        pts.append((x, y, pt))

    chg = weekly.get("week_chg_pct")
    line_color = _pct_color(chg)
    point_nodes = "".join(
        f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4.2' fill='{line_color}'/>"
        for x, y, _pt in pts
    )
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
    last_x, last_y, _ = pts[-1]
    start_text = weekly.get("week_start_date", "")
    end_text = weekly.get("week_end_date", "")
    prev_note = ""
    if weekly.get("prev_close_chg_pct") is not None:
        prev_note = f"｜相對上週五收盤 {pct_text(weekly.get('prev_close_chg_pct'))}"
    return f"""
    <svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="本週週一至週五價格走勢">
      <rect x="0" y="0" width="{width}" height="{height}" rx="18" fill="#fffdf7"/>
      <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" stroke="#d8d1bd"/>
      <line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" y2="{pad_t + plot_h}" stroke="#d8d1bd"/>
      <polyline points="{poly}" fill="none" stroke="{line_color}" stroke-width="5.2" stroke-linecap="round" stroke-linejoin="round"/>
      {point_nodes}
      <text x="{pad_l}" y="18" fill="#6f776f" font-size="12">高 {max(values):.2f}</text>
      <text x="{pad_l}" y="{pad_t + plot_h - 6}" fill="#6f776f" font-size="12">低 {min(values):.2f}</text>
      <rect x="{width - 244}" y="10" width="226" height="38" rx="10" fill="#fffdf7" stroke="#e0d7bd" opacity="0.96"/>
      <text x="{width - 28}" y="35" text-anchor="end" fill="{line_color}" font-size="20" font-weight="800">{clean[-1]['value']:.2f} / {pct_text(chg)}</text>
      <text x="{pad_l}" y="{height - 2}" fill="#6f776f" font-size="12">本週定義：週一開盤至週五收盤（{start_text} - {end_text}）{prev_note}</text>
    </svg>"""


def _series_delta(values: list) -> float | None:
    clean = []
    for value in values or []:
        try:
            clean.append(float(value))
        except Exception:
            continue
    if len(clean) < 2:
        return None
    return clean[-1] - clean[0]


def macro_metric_note(kind: str, value, series: list | None = None) -> str:
    delta = _series_delta(series or [])
    if kind == "institutional":
        if value is None:
            return "法人資金資料不足；先以價格、量能與均線判斷市場方向。"
        if value < 0:
            return "法人週累計偏賣超，代表大型資金本週降低風險。若同時遇到美中政治、地緣風險或全球股市高檔震盪，權值股追價意願通常下降；若融資餘額也在高檔，拉回時波動容易放大。"
        if value > 0:
            return "法人週累計偏買超，代表大型資金仍願意承接台股。若全球股市風險偏好維持、美元壓力不升，權值股較容易延續主流行情；但融資餘額高檔時仍要避免過度追價。"
        return "法人週累計接近平衡，代表資金沒有明確方向；下週要看是否轉為連續買超或賣超。"
    if kind == "fx":
        if delta is None:
            return "匯率週資料不足；美元/台幣仍是外資風險偏好的重要觀察點。"
        if delta > 0:
            return "美元/台幣走升代表台幣轉弱，常見於美元偏強、國際政治或地緣風險升溫時；對外資回流與台股評價通常偏壓抑。"
        if delta < 0:
            return "美元/台幣走低代表台幣轉強，通常有利外資評估台股匯兌風險；若搭配法人買超，對權值股較正面。"
        return "美元/台幣變化有限，匯率暫時不是本週台股主要壓力來源。"
    if kind == "rates":
        if delta is None:
            return "美債殖利率週資料不足；仍需觀察對科技股評價的影響。"
        if delta > 0:
            return "美10年債走升代表全球資金折現率壓力升高，對 AI、半導體等高評價族群較不利；若全球股市同時過熱，容易引發評價修正。"
        if delta < 0:
            return "美10年債走低代表估值壓力緩和，通常有利科技股與成長股；若地緣風險沒有升高，台股權值股承接會較穩。"
        return "美10年債變化有限，利率端暫時沒有提供明顯方向。"
    return ""


def sort_weekly_results(results: list, include_market: bool = True) -> list:
    if not results:
        return []
    market = []
    stocks = results
    if results[0][1] == "^TWII":
        market = [results[0]]
        stocks = results[1:]
    stocks = sorted(stocks, key=lambda item: item[2].get("weekly", {}).get("week_chg_pct") or 0, reverse=True)
    return (market if include_market else []) + stocks


def weekly_market_overview_html(results: list, macro: dict | None, compact: bool = False) -> str:
    if not results:
        return ""
    market = results[0][2]
    weekly = market.get("weekly", {})
    fx = macro.get("fx") if macro else None
    rates = macro.get("rates") if macro else None
    chart_w = 920 if compact else 650
    chart_h = 360 if compact else 280
    chart = render_week_price_chart(market, chart_w, chart_h)
    fx_metric = f"{fx['value']:.3f}" if fx else "-"
    rates_metric = f"{rates['value']:.2f}%" if rates else "-"
    inst_value_text = weekly.get("institutional_value_text") or format_twd_billion_short(weekly.get("institutional_value"))
    inst_series = weekly.get("institutional_daily_values", [])
    fx_series = fx.get("series", []) if fx else []
    rates_series = rates.get("series", []) if rates else []
    gap_note = week_gap_note(weekly)
    range_note = weekly.get("range_position_note") or range_position_note(weekly.get("range_pos"))
    inst_note = macro_metric_note("institutional", weekly.get("institutional_value"), inst_series)
    fx_note = macro_metric_note("fx", fx.get("value") if fx else None, fx_series)
    rates_note = macro_metric_note("rates", rates.get("value") if rates else None, rates_series)
    metric_style = "background:#f8f4e7;border:1px solid #e5dcc0;border-radius:10px;padding:10px 12px;"
    return (
        f'<div style="background:{WEEKLY_PANEL};border:1px solid #e0d7bd;border-radius:14px;padding:18px 20px;margin-bottom:24px;">'
        f'<div style="display:flex;justify-content:space-between;gap:18px;align-items:flex-start;">'
        f'<div style="min-width:0;flex:1;">'
        f'<div style="color:{WEEKLY_GOLD};font-size:12px;font-weight:bold;letter-spacing:.08em;">WEEKLY MARKET BRIEF</div>'
        f'<div style="color:{WEEKLY_DARK};font-size:24px;font-weight:800;margin-top:4px;">{_escape(weekly.get("posture", "觀察"))}</div>'
        f'<div style="color:#4f5a52;font-size:14px;line-height:1.7;margin-top:8px;">{_escape(weekly.get("trend_summary", ""))}<br>{_escape(weekly.get("next_focus", ""))}</div>'
        f'</div>'
        f'<div style="text-align:right;white-space:nowrap;">'
        f'<div style="color:#6f776f;font-size:12px;">加權指數收盤</div>'
        f'<div style="color:{WEEKLY_DARK};font-size:30px;font-weight:800;">{market.get("close", 0):.2f}</div>'
        f'<div style="color:{_pct_color(weekly.get("week_chg_pct"))};font-size:16px;font-weight:800;">{pct_text(weekly.get("week_chg_pct"))}</div>'
        f'</div></div>'
        f'<div style="margin-top:16px;">{chart}</div>'
        f'<div style="background:#fbf7ea;border-left:4px solid {WEEKLY_GOLD};padding:9px 12px;margin-top:10px;color:#4f5a52;font-size:12px;line-height:1.6;">'
        f'<b>口徑說明：</b>本週漲跌＝週一開盤到週五收盤；相對上週五＝上週五收盤到週五收盤。{_escape(gap_note)}</div>'
        f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px;">'
        f'<div style="{metric_style}"><div style="font-size:11px;color:#7a8178;">本週高 / 低 / 收盤位置</div><div style="font-size:15px;font-weight:800;color:{WEEKLY_DARK};">{weekly.get("week_high", 0):.2f} / {weekly.get("week_low", 0):.2f} / {weekly.get("range_pos", 0):.0f}%</div><div style="font-size:11px;color:#7a8178;line-height:1.45;margin-top:4px;">{_escape(range_note)}</div></div>'
        f'<div style="{metric_style}"><div style="font-size:11px;color:#7a8178;">法人週累計（金額）</div><div style="font-size:15px;font-weight:800;color:{_pct_color(weekly.get("institutional_value"))};">{inst_value_text}</div>{render_sparkline(inst_series, 118, 28)}<div style="font-size:11px;color:#7a8178;line-height:1.45;margin-top:4px;">{_escape(inst_note)}</div></div>'
        f'<div style="{metric_style}"><div style="font-size:11px;color:#7a8178;">美元/台幣</div><div style="font-size:15px;font-weight:800;color:{WEEKLY_DARK};">{fx_metric}</div>{render_sparkline(fx_series, 118, 28)}<div style="font-size:11px;color:#7a8178;line-height:1.45;margin-top:4px;">{_escape(fx_note)}</div></div>'
        f'<div style="{metric_style}"><div style="font-size:11px;color:#7a8178;">美10年債</div><div style="font-size:15px;font-weight:800;color:{WEEKLY_DARK};">{rates_metric}</div>{render_sparkline(rates_series, 118, 28)}<div style="font-size:11px;color:#7a8178;line-height:1.45;margin-top:4px;">{_escape(rates_note)}</div></div>'
        f'</div></div>'
    )


def weekly_stock_scoreboard_html(results: list) -> str:
    sorted_results = sort_weekly_results(results, include_market=False)
    rows = ""
    max_abs = max([abs(item[2].get("weekly", {}).get("week_chg_pct") or 0) for item in sorted_results] + [1])
    for name, ticker, r in sorted_results:
        weekly = r.get("weekly", {})
        chg = weekly.get("week_chg_pct") or 0
        width = max(8, abs(chg) / max_abs * 100)
        color = UP_COLOR if chg >= 0 else DOWN_COLOR
        rows += (
            f'<div style="display:grid;grid-template-columns:92px 1fr 64px;gap:10px;align-items:center;margin:8px 0;">'
            f'<div style="font-size:13px;font-weight:800;color:{WEEKLY_DARK};">{_escape(name)}</div>'
            f'<div style="height:16px;background:#ebe6d6;border-radius:99px;overflow:hidden;"><div style="height:16px;width:{width:.1f}%;background:{color};border-radius:99px;"></div></div>'
            f'<div style="font-size:13px;font-weight:800;text-align:right;color:{color};">{pct_text(chg)}</div>'
            f'</div>'
        )
    return f'<div style="background:{WEEKLY_PANEL};border:1px solid #e0d7bd;border-radius:14px;padding:16px 18px;margin-bottom:24px;"><h3 style="margin:0 0 10px;color:{WEEKLY_DARK};font-size:18px;">權值股本週漲跌排名</h3>{rows}</div>'


def weekly_trend_matrix_html(results: list) -> str:
    rows = ""
    for name, ticker, r in sort_weekly_results(results, include_market=True):
        weekly = r.get("weekly", {})
        inst = weekly.get("institutional_value_text") or format_twd_billion_short(weekly.get("institutional_value"))
        vol = weekly.get("volume_ratio")
        vol_text = f"{vol:.2f}x" if vol is not None else "-"
        vol_tip = volume_ratio_note(vol)
        rows += (
            f'<tr style="border-bottom:1px solid #e7dfc9;">'
            f'<td style="padding:9px 8px;font-weight:800;color:{WEEKLY_DARK};">{_escape(name)}</td>'
            f'<td style="padding:9px 8px;color:{weekly.get("posture_color", WEEKLY_DARK)};font-weight:800;">{_escape(weekly.get("posture", "觀察"))}</td>'
            f'<td style="padding:9px 8px;text-align:right;color:{_pct_color(weekly.get("week_chg_pct"))};font-weight:800;">{pct_text(weekly.get("week_chg_pct"))}</td>'
            f'<td style="padding:9px 8px;color:#4f5a52;line-height:1.5;white-space:normal;">{_escape(weekly.get("ma_position", "-"))}</td>'
            f'<td style="padding:9px 8px;text-align:right;color:{_pct_color(weekly.get("institutional_value"))};font-weight:800;">{_escape(inst)}</td>'
            f'<td style="padding:9px 8px;text-align:right;color:#4f5a52;" title="{_escape(vol_tip)}">{vol_text}</td>'
            f'</tr>'
        )
    return (
        f'<div style="background:{WEEKLY_PANEL};border:1px solid #e0d7bd;border-radius:14px;padding:16px 18px;margin-bottom:24px;">'
        f'<h3 style="margin:0 0 10px;color:{WEEKLY_DARK};font-size:18px;">趨勢矩陣</h3>'
        f'<table style="width:100%;border-collapse:collapse;font-size:12px;">'
        f'<thead><tr style="background:#efe7cf;color:{WEEKLY_DARK};"><th style="padding:8px;text-align:left;width:90px;">標的</th><th style="padding:8px;text-align:left;width:80px;">狀態</th><th style="padding:8px;text-align:right;width:64px;">本週</th><th style="padding:8px;text-align:left;">均線</th><th style="padding:8px;text-align:right;width:100px;">法人金額</th><th style="padding:8px;text-align:right;width:70px;">量能</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )

def weekly_stock_detail_block(name: str, ticker: str, result: dict) -> str:
    weekly = result.get("weekly", {})
    code = ticker.replace(".TW", "").replace(".tw", "")
    week_pct = weekly.get("week_chg_pct")
    prev_pct = weekly.get("prev_close_chg_pct")
    inst_text = weekly.get("institutional_value_text") or format_twd_billion_short(weekly.get("institutional_value"))
    vol = weekly.get("volume_ratio")
    vol_text = f"{vol:.2f}x" if vol is not None else "-"
    vol_note = volume_ratio_note(vol)
    border = weekly.get("posture_color", result.get("border", WEEKLY_DARK))
    cards = [
        ("本週漲跌", pct_text(week_pct), _pct_color(week_pct), f"週一開盤 {weekly.get('week_start_open', 0):.2f} → 週五收盤 {result.get('close', 0):.2f}"),
        ("相對上週五", pct_text(prev_pct), _pct_color(prev_pct), week_gap_note(weekly)),
        ("高低與位置", f"{weekly.get('week_high', 0):.2f} / {weekly.get('week_low', 0):.2f} / {weekly.get('range_pos', 0):.0f}%", WEEKLY_DARK, weekly.get("range_position_note") or range_position_note(weekly.get("range_pos"))),
        ("法人金額", inst_text, _pct_color(weekly.get("institutional_value")), "大盤為證交所金額；個股為張數乘收盤價估算"),
        ("量能", vol_text, WEEKLY_DARK, vol_note),
    ]
    card_html = "".join(
        f'<td style="width:20%;padding:8px;vertical-align:top;">'
        f'<div style="background:#f8f4e7;border:1px solid #e5dcc0;border-radius:10px;padding:10px;min-height:76px;">'
        f'<div style="font-size:11px;color:#7a8178;margin-bottom:5px;">{html_lib.escape(label)}</div>'
        f'<div style="font-size:15px;font-weight:800;color:{color};line-height:1.3;">{html_lib.escape(value)}</div>'
        f'<div style="font-size:11px;color:#7a8178;line-height:1.45;margin-top:4px;">{html_lib.escape(note)}</div>'
        f'</div></td>'
        for label, value, color, note in cards
    )
    return (
        f'<div style="background:{WEEKLY_PANEL};border:1px solid #e0d7bd;border-left:5px solid {border};border-radius:12px;padding:14px 16px;margin-bottom:14px;">'
        f'<div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;">'
        f'<div><div style="font-size:17px;font-weight:800;color:{WEEKLY_DARK};">{html_lib.escape(name)}</div>'
        f'<div style="font-size:12px;color:#7a8178;">{html_lib.escape(code)}｜統計區間 {html_lib.escape(weekly.get("week_range_label", ""))}</div></div>'
        f'<div style="text-align:right;"><div style="font-size:17px;font-weight:800;color:{WEEKLY_DARK};">{result.get("close", 0):.2f}</div>'
        f'<div style="font-size:12px;font-weight:800;color:{border};">{html_lib.escape(weekly.get("posture", "觀察"))}</div></div></div>'
        f'<div style="font-size:13px;color:#4f5a52;line-height:1.7;margin:10px 0 12px;">{html_lib.escape(weekly.get("next_focus", ""))}</div>'
        f'<table style="width:100%;border-collapse:separate;border-spacing:0;"><tr>{card_html}</tr></table>'
        f'<div style="font-size:12px;color:#4f5a52;line-height:1.6;margin-top:10px;"><b>均線位置：</b>{html_lib.escape(weekly.get("ma_position", "-"))}</div>'
        f'</div>'
    )


# ── 組裝 HTML Email ──────────────────────────────────────────
# 保留 Email 路徑：目前可停用，但不可在重構時移除。
def build_email_html(results: list, today: str, cfg: dict | None = None,
                     macro: dict | None = None, news_items: list | None = None,
                     event_items: list | None = None) -> str:
    meta = get_report_meta(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ))
    market_brief = weekly_market_overview_html(results, macro)
    scoreboard = weekly_stock_scoreboard_html(results)
    matrix = weekly_trend_matrix_html(results)
    recent_news_items = list(news_items or [])
    if not recent_news_items and results:
        market_name, _market_ticker, market_result = results[0]
        market_weekly = market_result.get("weekly", {})
        recent_news_items.append({
            "date": today,
            "title": f"{market_name}本週盤勢回顧：{pct_text(market_weekly.get('week_chg_pct'))}",
            "impact": "中",
            "scope": "加權指數、權值股、法人資金",
            "note": f"本週收盤位於高低區間{market_weekly.get('range_pos', 0):.0f}%；法人週累計{market_weekly.get('institutional_value_text', '-')}。若自動新聞來源暫時無資料，先以價格、量能與法人資金作為市場回顧。",
            "source": "系統盤勢摘要",
            "link": "",
        })
    events_block = market_events_html(cfg or {}, today, event_items or [], recent_news_items)
    rules_block = scoring_rules_html()
    sorted_details = sort_weekly_results(results, include_market=True)
    details = "".join(
        weekly_stock_detail_block(n, t, r)
        for n, t, r in sorted_details
    )
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        f'<body style="font-family:Arial,sans-serif;max-width:760px;margin:0 auto;padding:20px;background:{WEEKLY_BG};">'
        f'<div style="background:{WEEKLY_DARK};color:#fff;padding:24px 26px;border-radius:14px 14px 0 0;">'
        f'<div style="color:{WEEKLY_GOLD};font-size:12px;font-weight:bold;letter-spacing:.12em;">TAIWAN EQUITY WEEKLY</div>'
        f'<h2 style="margin:6px 0 0;font-size:28px;line-height:1.25;">每週台股趨勢報告</h2>'
        f'<p style="margin:8px 0 0;color:#d9d2bd;">{today}｜{meta["week_label"]}｜一週總結、方向整理、下週觀察</p></div>'
        f'<div style="background:#fffaf0;padding:24px;border-radius:0 0 14px 14px;box-shadow:0 8px 24px rgba(18,50,43,.10);">'
        f'{market_brief}'
        f'{scoreboard}'
        f'{matrix}'
        f'{events_block}'
        f'<h3 style="color:{WEEKLY_DARK};border-bottom:2px solid {WEEKLY_GOLD};padding-bottom:6px;">指標解讀規則</h3>'
        f'{rules_block}'
        f'<h3 style="color:{WEEKLY_DARK};border-bottom:2px solid {WEEKLY_GOLD};padding-bottom:6px;">各股指標明細</h3>'
        f'{details}'
        f'<p style="color:#9a927e;font-size:11px;text-align:center;border-top:1px solid #e6ddc7;padding-top:12px;margin-top:8px;">'
        f'本報告由自動化模型產生，僅供參考，不構成投資建議。</p>'
        f'</div></body></html>'
    )

def _social_item_detail(result: dict, label: str, default: str = "-") -> tuple[str, str, str]:
    for item_label, value, color, note in result.get("items", []):
        if item_label == label or item_label.startswith(label):
            return str(value), str(color), str(note)
    return default, NEUTRAL_COLOR, ""


def _social_item(result: dict, label: str, default: str = "-") -> str:
    value, _color, _note = _social_item_detail(result, label, default)
    return value


def _social_reason(result: dict, limit: int = 78) -> str:
    trade_plan = result.get("trade_plan", {})
    reason = trade_plan.get("reason") or result.get("advice", "")
    reason = re.sub(r"\s+", " ", str(reason)).strip()
    return html_lib.escape(reason[:limit] + ("..." if len(reason) > limit else ""))


def _social_short_text(value: str, limit: int = 34) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit] + ("..." if len(value) > limit else "")


def _social_events(event_items: list | None, today: str, limit: int = 4) -> list[dict]:
    today_date = datetime.strptime(today, "%Y-%m-%d").date()
    filtered = []
    for event in event_items or []:
        try:
            event_date = datetime.strptime(event.get("date", ""), "%Y-%m-%d").date()
        except Exception:
            event_date = today_date
        item = dict(event)
        item["_distance"] = abs((event_date - today_date).days)
        filtered.append(item)
    impact_rank = {"高": 4, "中高": 3, "中": 2, "低": 1}
    filtered.sort(key=lambda x: (x["_distance"], -impact_rank.get(x.get("impact", ""), 0), x.get("date", "")))
    for item in filtered:
        item.pop("_distance", None)
    return filtered[:limit]
def _social_indicator_tile(result: dict, label: str, title: str | None = None) -> str:
    value, color, note = _social_item_detail(result, label)
    title = title or label
    note_text = ""
    if note:
        note_text = _social_short_text(note.split("｜")[0], 24)
    return (
        f"<div class='ind'>"
        f"<div class='ind-title'>{html_lib.escape(title)}</div>"
        f"<div class='ind-value' style='color:{color}'>{html_lib.escape(_social_short_text(value, 16))}</div>"
        f"<div class='ind-note'>{html_lib.escape(note_text)}</div>"
        f"</div>"
    )


def _social_score_impact(note: str) -> tuple[float, float]:
    match = re.search(r"分數影響:正向條件\+([0-9.]+)\/風險條件\+([0-9.]+)", note or "")
    if not match:
        return 0.0, 0.0
    return float(match.group(1)), float(match.group(2))


def _social_key_indicator_tiles(result: dict, limit: int = 2) -> str:
    priority = {
        "趨勢環境": 90,
        "MACD": 80,
        "三大法人": 70,
        "美元/台幣匯率": 62,
        "利率環境": 60,
        "KD": 55,
        "OBV": 50,
        "量能趨勢": 45,
        "均線交叉": 40,
        "價格行為": 10,
    }
    candidates = []
    for label, value, color, note in result.get("items", []):
        if label in ("BIAS60 Z-Score", "⚠️ 槓桿警示"):
            continue
        buy, sell = _social_score_impact(str(note))
        score = max(buy, sell)
        if score <= 0 and label not in ("趨勢環境", "MACD"):
            continue
        candidates.append((score, priority.get(label, 0), str(label), str(value), str(color), str(note)))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picked = candidates[:limit]
    existing = {item[2] for item in picked}
    for fallback in ("趨勢環境", "MACD", "三大法人", "KD"):
        if len(picked) >= limit:
            break
        if fallback in existing:
            continue
        value, color, note = _social_item_detail(result, fallback)
        if value != "-":
            picked.append((0.0, priority.get(fallback, 0), fallback, value, color, note))
            existing.add(fallback)

    tiles = ""
    title_map = {"趨勢環境": "趨勢", "三大法人": "法人", "美元/台幣匯率": "匯率", "利率環境": "利率", "量能趨勢": "量能"}
    for _score, _priority, label, value, color, note in picked[:limit]:
        note_text = _social_short_text(str(note).split("｜")[0], 24) if note else ""
        tiles += (
            f"<div class='ind'>"
            f"<div class='ind-title'>{html_lib.escape(title_map.get(label, label))}</div>"
            f"<div class='ind-value' style='color:{color}'>{html_lib.escape(_social_short_text(value, 16))}</div>"
            f"<div class='ind-note'>{html_lib.escape(note_text)}</div>"
            f"</div>"
        )
    return tiles


def build_social_report_pages(results: list, today: str, cfg: dict | None = None,
                              macro: dict | None = None, news_items: list | None = None,
                              event_items: list | None = None) -> list[str]:
    news_items = news_items or []
    date_text = today.replace("-", "/")
    meta = get_report_meta(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ))
    market = results[0][2] if results else {}
    market_weekly = market.get("weekly", {})
    fx = macro.get("fx") if macro else None
    rates = macro.get("rates") if macro else None
    fx_value = f"{fx['value']:.3f}" if fx else "-"
    rates_value = f"{rates['value']:.2f}%" if rates else "-"
    inst_value_text = market_weekly.get("institutional_value_text") or format_twd_billion_short(market_weekly.get("institutional_value"))
    inst_series = market_weekly.get("institutional_daily_values", [])
    fx_series = fx.get("series", []) if fx else []
    rates_series = rates.get("series", []) if rates else []
    inst_note = macro_metric_note("institutional", market_weekly.get("institutional_value"), inst_series)
    fx_note = macro_metric_note("fx", fx.get("value") if fx else None, fx_series)
    rates_note = macro_metric_note("rates", rates.get("value") if rates else None, rates_series)
    gap_note = week_gap_note(market_weekly)
    range_note = market_weekly.get("range_position_note") or range_position_note(market_weekly.get("range_pos"))
    chart = render_week_price_chart(market, 890, 330)
    tracked = sort_weekly_results(results, include_market=True)
    stock_results = sort_weekly_results(results, include_market=False)

    css = f"""
    <style>
      *{{box-sizing:border-box}} body{{margin:0;background:#e8e1d0;font-family:Arial,'Noto Sans TC',sans-serif;color:#26322d}}
      .page{{width:1080px;height:1920px;background:{WEEKLY_BG};padding:48px 58px;overflow:hidden}}
      .header{{background:{WEEKLY_DARK};color:#fff;border-radius:22px;padding:30px 34px;margin-bottom:22px;border-bottom:8px solid {WEEKLY_GOLD}}}
      .kicker{{color:{WEEKLY_GOLD};font-size:18px;font-weight:800;letter-spacing:.12em}}.title{{font-size:48px;font-weight:900;line-height:1.12;margin-top:8px}}.date{{font-size:22px;color:#d9d2bd;margin-top:8px}}
      .section{{background:{WEEKLY_PANEL};border:1px solid #ded4b8;border-radius:20px;padding:22px 26px;margin-bottom:18px;box-shadow:0 8px 22px rgba(18,50,43,.07)}}
      .section-title{{font-size:29px;font-weight:900;color:{WEEKLY_DARK};margin-bottom:14px}}
      .market-top{{display:grid;grid-template-columns:1.15fr .85fr;gap:18px;align-items:start}}.summary{{font-size:36px;font-weight:900;color:{market_weekly.get('posture_color', WEEKLY_DARK)};line-height:1.12}}.summary-sub{{font-size:21px;line-height:1.48;color:#4f5a52;margin-top:10px}}
      .market-number{{text-align:right}}.market-label{{font-size:17px;color:#7a8178}}.market-close{{font-size:46px;font-weight:900;color:{WEEKLY_DARK};line-height:1.08}}.market-chg{{font-size:27px;font-weight:900;color:{_pct_color(market_weekly.get('week_chg_pct'))}}}
      .note{{background:#fbf7ea;border-left:6px solid {WEEKLY_GOLD};border-radius:10px;padding:10px 13px;color:#4f5a52;font-size:17px;line-height:1.45;margin-top:12px}}
      .metric-grid{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px;margin-top:14px}}.metric{{background:#f4edd9;border:1px solid #e2d6b9;border-radius:14px;padding:13px 14px;min-height:154px}}.metric-label{{font-size:15px;color:#7a8178}}.metric-value{{font-size:25px;font-weight:900;color:{WEEKLY_DARK};margin-top:4px;line-height:1.1}}.metric-note{{font-size:13px;color:#67736c;line-height:1.35;margin-top:6px;max-height:72px;overflow:hidden}}
      .event-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.event{{border-left:8px solid var(--c);background:#fbf7eb;border-radius:14px;padding:13px 15px;min-height:106px}}.event-title{{font-size:20px;font-weight:900;line-height:1.28;color:{WEEKLY_DARK}}}.event-note{{font-size:16px;line-height:1.35;color:#536158;margin-top:7px}}
      .bars{{display:grid;gap:11px}}.bar-row{{display:grid;grid-template-columns:118px 1fr 78px;gap:12px;align-items:center}}.bar-name{{font-size:21px;font-weight:900;color:{WEEKLY_DARK}}}.bar-track{{height:22px;background:#e8dfc8;border-radius:999px;overflow:hidden}}.bar-fill{{height:22px;border-radius:999px;background:var(--c);width:var(--w)}}.bar-val{{font-size:20px;font-weight:900;text-align:right;color:var(--c)}}
      .matrix{{display:grid;grid-template-columns:1fr 1fr;gap:13px}}.stock-card{{background:#fffdf7;border:1px solid #ded4b8;border-left:10px solid var(--c);border-radius:16px;padding:14px 15px;min-height:186px}}.stock-head{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}.stock-name{{font-size:25px;font-weight:900;color:{WEEKLY_DARK}}}.stock-code{{font-size:15px;color:#7a8178;margin-top:2px}}.stock-price{{font-size:25px;font-weight:900;color:#24352f;text-align:right}}.stock-status{{font-size:16px;font-weight:900;color:var(--c);text-align:right;margin-top:4px}}.stock-note{{font-size:15px;color:#536158;line-height:1.35;margin-top:8px;max-height:40px;overflow:hidden}}
      .tile-row{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px}}.tile{{background:#f4edd9;border-radius:10px;padding:8px}}.tile-label{{font-size:12px;color:#7a8178}}.tile-value{{font-size:15px;font-weight:900;color:{WEEKLY_DARK};margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.tile-note{{font-size:11px;color:#7a8178;line-height:1.22;margin-top:3px;max-height:28px;overflow:hidden}}
      .footer{{font-size:16px;color:#7a8178;text-align:center;margin-top:10px}}
    </style>
    """

    impact_colors = {"高": UP_COLOR, "中高": WARN_COLOR, "中": "#b8871b", "低": NEUTRAL_COLOR}
    event_items = _social_events(event_items, today, 4)
    if not event_items:
        event_items.append({
            "date": today,
            "title": "自動重大事件掃描",
            "impact": "中",
            "note": "過去 5 天至未來 30 天內尚未掃描到高關聯重大事件。",
        })
    event_rows = "".join(
        f"<div class='event' style='--c:{impact_colors.get(e.get('impact',''), NEUTRAL_COLOR)}'><div class='event-title'>{html_lib.escape(_social_short_text(e.get('title',''), 36))}</div><div class='event-note'>{html_lib.escape(e.get('date',''))}｜{html_lib.escape(_social_short_text(e.get('note',''), 66))}</div></div>"
        for e in event_items[:4]
    )
    metric_html = f"""
      <div class='metric-grid'>
        <div class='metric'><div class='metric-label'>本週高 / 低 / 收盤位置</div><div class='metric-value'>{market_weekly.get('week_high', 0):.0f}/{market_weekly.get('week_low', 0):.0f}/{market_weekly.get('range_pos', 0):.0f}%</div><div class='metric-note'>{html_lib.escape(_social_short_text(range_note, 58))}</div></div>
        <div class='metric'><div class='metric-label'>法人週累計（金額）</div><div class='metric-value' style='color:{_pct_color(market_weekly.get('institutional_value'))}'>{inst_value_text}</div>{render_sparkline(inst_series, 150, 32)}<div class='metric-note'>{html_lib.escape(_social_short_text(inst_note, 58))}</div></div>
        <div class='metric'><div class='metric-label'>美元/台幣</div><div class='metric-value'>{fx_value}</div>{render_sparkline(fx_series, 150, 32)}<div class='metric-note'>{html_lib.escape(_social_short_text(fx_note, 58))}</div></div>
        <div class='metric'><div class='metric-label'>美10年債</div><div class='metric-value'>{rates_value}</div>{render_sparkline(rates_series, 150, 32)}<div class='metric-note'>{html_lib.escape(_social_short_text(rates_note, 58))}</div></div>
      </div>"""
    page1 = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>{css}</head><body><div class='page'>
      <div class='header'><div class='kicker'>TAIWAN EQUITY WEEKLY</div><div class='title'>每週台股趨勢報告</div><div class='date'>{date_text}｜{meta['week_label']}｜本週變化、趨勢判斷、下週觀察</div></div>
      <div class='section'><div class='market-top'><div><div class='summary'>{html_lib.escape(market_weekly.get('posture','觀察'))}</div><div class='summary-sub'>{html_lib.escape(market_weekly.get('trend_summary',''))}<br>{html_lib.escape(market_weekly.get('next_focus',''))}</div></div><div class='market-number'><div class='market-label'>加權指數收盤</div><div class='market-close'>{market.get('close', 0):.2f}</div><div class='market-chg'>{pct_text(market_weekly.get('week_chg_pct'))}</div></div></div><div class='note'><b>口徑說明：</b>本週漲跌＝週一開盤到週五收盤；相對上週五＝上週五收盤到週五收盤。{html_lib.escape(gap_note)}</div>{metric_html}</div>
      <div class='section'><div class='section-title'>加權指數本週走勢</div>{chart}</div>
      <div class='section'><div class='section-title'>重大事件與市場脈絡</div><div class='event-grid'>{event_rows}</div></div>
      <div class='footer'>本圖由自動化模型產生，僅供參考，不構成投資建議。</div>
    </div></body></html>"""

    max_abs = max([abs(item[2].get('weekly', {}).get('week_chg_pct') or 0) for item in stock_results] + [1])
    bars = ""
    for name, ticker, r in stock_results:
        weekly = r.get('weekly', {})
        chg = weekly.get('week_chg_pct') or 0
        width = max(8, abs(chg) / max_abs * 100)
        color = _pct_color(chg)
        bars += f"<div class='bar-row'><div class='bar-name'>{html_lib.escape(name)}</div><div class='bar-track'><div class='bar-fill' style='--c:{color};--w:{width:.1f}%'></div></div><div class='bar-val' style='--c:{color}'>{pct_text(chg)}</div></div>"
    cards = ""
    for name, ticker, r in stock_results:
        weekly = r.get('weekly', {})
        inst_text = weekly.get('institutional_value_text') or format_twd_billion_short(weekly.get('institutional_value'))
        vol = weekly.get('volume_ratio')
        vol_text = f"{vol:.2f}x" if vol is not None else "-"
        color = weekly.get('posture_color', r.get('border', NEUTRAL_COLOR))
        cards += (
            f"<div class='stock-card' style='--c:{color}'><div class='stock-head'><div><div class='stock-name'>{html_lib.escape(name)}</div><div class='stock-code'>{ticker.replace('.TW','').replace('.tw','')}｜{html_lib.escape(weekly.get('week_range_label',''))}</div></div><div><div class='stock-price'>{r.get('close',0):.2f}</div><div class='stock-status'>{html_lib.escape(weekly.get('posture','觀察'))}</div></div></div>"
            f"<div class='stock-note'>{html_lib.escape(_social_short_text(weekly.get('next_focus',''), 66))}</div>"
            f"<div class='tile-row'><div class='tile'><div class='tile-label'>本週</div><div class='tile-value' style='color:{_pct_color(weekly.get('week_chg_pct'))}'>{pct_text(weekly.get('week_chg_pct'))}</div><div class='tile-note'>週一開盤至週五收盤</div></div><div class='tile'><div class='tile-label'>收盤位置</div><div class='tile-value'>{weekly.get('range_pos',0):.0f}%</div><div class='tile-note'>0%=低 / 100%=高</div></div><div class='tile'><div class='tile-label'>法人金額</div><div class='tile-value' style='color:{_pct_color(weekly.get('institutional_value'))}'>{html_lib.escape(inst_text)}</div><div class='tile-note'>大盤為官方金額</div></div></div><div class='tile-row'><div class='tile'><div class='tile-label'>相對上週五</div><div class='tile-value' style='color:{_pct_color(weekly.get('prev_close_chg_pct'))}'>{pct_text(weekly.get('prev_close_chg_pct'))}</div></div><div class='tile'><div class='tile-label'>量能</div><div class='tile-value'>{vol_text}</div><div class='tile-note'>{html_lib.escape(_social_short_text(volume_ratio_note(vol), 24))}</div></div><div class='tile'><div class='tile-label'>均線</div><div class='tile-value'>{html_lib.escape(_social_short_text(weekly.get('ma_position','-'), 18))}</div></div></div></div>"
        )
    page2 = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>{css}</head><body><div class='page'>
      <div class='header'><div class='kicker'>LARGE CAP MAP</div><div class='title'>權值股週變化地圖</div><div class='date'>{date_text}｜依本週漲跌排序｜趨勢分層與下週觀察</div></div>
      <div class='section'><div class='section-title'>權值股本週漲跌排名</div><div class='bars'>{bars}</div></div>
      <div class='section'><div class='section-title'>趨勢矩陣</div><div class='matrix'>{cards}</div></div>
      <div class='footer'>完整指標與評分細節請以 Email 報告為準。</div>
    </div></body></html>"""
    return [page1, page2]


def save_social_report_pages(pages: list[str], today: str) -> list[Path]:
    paths = []
    date_key = today.replace("-", "")
    for idx, html in enumerate(pages, start=1):
        path = Path(__file__).parent / f"social_report_{idx:02d}.html"
        path.write_text(html, encoding="utf-8")
        paths.append(path)
    return paths


def _public_signal_color(result: dict) -> str:
    weekly = result.get("weekly", {})
    return weekly.get("posture_color") or result.get("border") or NEUTRAL_COLOR


def _public_action_text(result: dict) -> str:
    plan = result.get("trade_plan", {})
    headline = str(plan.get("headline") or "觀察")
    return headline.replace("維持觀察", "觀察")


def _public_news_items(news_items: list | None, limit: int = 4) -> str:
    rows = ""
    for item in (news_items or [])[:limit]:
        title = html_lib.escape(_social_short_text(item.get("title", ""), 42))
        note = html_lib.escape(_social_short_text(item.get("note", item.get("source", "")), 68))
        date = html_lib.escape(str(item.get("date", "")))
        rows += f"<div class='mini'><b>{title}</b><span>{date}｜{note}</span></div>"
    if rows:
        return rows
    return "<div class='mini'><b>近期新聞</b><span>本週以價格、法人、匯率與利率資料作為主要判斷，新聞來源暫無高關聯摘要。</span></div>"


def _public_event_items(event_items: list | None, today: str, limit: int = 4) -> str:
    events = _social_events(event_items, today, limit)
    if not events:
        events = [{
            "date": today,
            "title": "自動重大事件掃描",
            "note": "過去 5 天至未來 30 天內尚未掃描到高關聯重大事件。",
            "impact": "中",
        }]
    rows = ""
    for item in events[:limit]:
        color = {"高": UP_COLOR, "中高": WARN_COLOR, "中": "#b8871b", "低": NEUTRAL_COLOR}.get(item.get("impact"), NEUTRAL_COLOR)
        title = html_lib.escape(_social_short_text(item.get("title", ""), 40))
        note = html_lib.escape(_social_short_text(item.get("note", ""), 70))
        date = html_lib.escape(str(item.get("date", "")))
        rows += f"<div class='mini event-mini' style='--c:{color}'><b>{title}</b><span>{date}｜{note}</span></div>"
    return rows


def _public_indicator_tiles(result: dict) -> str:
    labels = [
        ("季線支撐位置", "季線"),
        ("BIAS60 Z-Score", "BIAS60"),
        ("趨勢環境", "趨勢"),
        ("本週三大法人", "法人"),
        ("MACD", "MACD"),
        ("KD", "KD"),
        ("量能趨勢", "量能"),
        ("OBV", "OBV"),
    ]
    tiles = ""
    for label, title in labels:
        value, color, note = _social_item_detail(result, label)
        if value == "-" and label == "本週三大法人":
            value, color, note = _social_item_detail(result, "三大法人")
        tiles += (
            f"<div class='radar-tile'>"
            f"<div class='radar-label'>{html_lib.escape(title)}</div>"
            f"<div class='radar-value' style='color:{color}'>{html_lib.escape(_social_short_text(value, 18))}</div>"
            f"<div class='radar-note'>{html_lib.escape(_social_short_text(str(note).split('｜')[0], 34))}</div>"
            f"</div>"
        )
    return tiles


def build_public_report_html(results: list, today: str, cfg: dict | None = None,
                             macro: dict | None = None, news_items: list | None = None,
                             event_items: list | None = None) -> str:
    date_text = today.replace("-", "/")
    meta = get_report_meta(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ))
    market = results[0][2] if results else {}
    market_weekly = market.get("weekly", {})
    tracked_results = sort_weekly_results(results, include_market=True)
    stock_results = sort_weekly_results(results, include_market=False)
    market_chart = render_week_price_chart(market, 790, 250)
    fx = macro.get("fx") if macro else None
    rates = macro.get("rates") if macro else None
    fx_value = f"{fx['value']:.3f}" if fx else "-"
    rates_value = f"{rates['value']:.2f}%" if rates else "-"
    inst_value_text = market_weekly.get("institutional_value_text") or format_twd_billion_short(market_weekly.get("institutional_value"))
    inst_series = market_weekly.get("institutional_daily_values", [])
    fx_series = fx.get("series", []) if fx else []
    rates_series = rates.get("series", []) if rates else []
    inst_note = macro_metric_note("institutional", market_weekly.get("institutional_value"), inst_series)
    fx_note = macro_metric_note("fx", fx.get("value") if fx else None, fx_series)
    rates_note = macro_metric_note("rates", rates.get("value") if rates else None, rates_series)

    max_abs = max([abs(r.get("weekly", {}).get("week_chg_pct") or 0) for _n, _t, r in stock_results] + [1])
    ranking = ""
    for name, ticker, result in tracked_results:
        weekly = result.get("weekly", {})
        chg = weekly.get("week_chg_pct") or 0
        width = max(9, abs(chg) / max_abs * 100)
        color = _pct_color(chg)
        ranking += (
            f"<div class='rank-row'><div class='rank-name'>{html_lib.escape(name)}</div>"
            f"<div class='rank-track'><div class='rank-fill' style='--w:{width:.1f}%;--c:{color}'></div></div>"
            f"<div class='rank-val' style='color:{color}'>{pct_text(chg)}</div></div>"
        )

    stock_cards = ""
    for name, ticker, result in stock_results:
        weekly = result.get("weekly", {})
        color = _public_signal_color(result)
        stock_cards += (
            f"<div class='stock-card' style='--c:{color}'>"
            f"<div class='stock-head'><div><b>{html_lib.escape(name)}</b><span>{ticker.replace('.TW','')}</span></div>"
            f"<div class='signal-dot'><span></span></div></div>"
            f"<div class='stock-line'><span>{html_lib.escape(weekly.get('posture','觀察'))}</span><strong style='color:{_pct_color(weekly.get('week_chg_pct'))}'>{pct_text(weekly.get('week_chg_pct'))}</strong></div>"
            f"<div class='stock-action'>{html_lib.escape(_public_action_text(result))}｜正向條件{result.get('effective_buy',0):.0f} / 風險條件{result.get('effective_sell',0):.0f}</div>"
            f"<p>{html_lib.escape(_social_short_text(result.get('trade_plan', {}).get('reason') or weekly.get('next_focus',''), 78))}</p>"
            f"</div>"
        )

    detail_pages = []
    for page_items in (tracked_results[:4], tracked_results[4:8]):
        detail_blocks = ""
        for name, ticker, result in page_items:
            weekly = result.get("weekly", {})
            color = _public_signal_color(result)
            detail_blocks += (
                f"<div class='detail-card' style='--c:{color}'>"
                f"<div class='detail-top'><div><b>{html_lib.escape(name)}</b><span>{ticker.replace('.TW','')}｜{html_lib.escape(weekly.get('week_range_label',''))}</span></div>"
                f"<div><strong>{result.get('close',0):.2f}</strong><em style='color:{_pct_color(weekly.get('week_chg_pct'))}'>{pct_text(weekly.get('week_chg_pct'))}</em></div></div>"
                f"<div class='detail-reason'>{html_lib.escape(_social_short_text(result.get('trade_plan', {}).get('reason') or weekly.get('next_focus',''), 118))}</div>"
                f"<div class='radar-grid'>{_public_indicator_tiles(result)}</div>"
                f"</div>"
            )
        detail_pages.append(detail_blocks)

    css = f"""
    <style>
      @page {{ size: 900px 1260px; margin: 0; }}
      *{{box-sizing:border-box}} body{{margin:0;background:{WEEKLY_BG};font-family:Arial,'Noto Sans TC',sans-serif;color:{WEEKLY_DARK}}}
      .page{{width:900px;height:1260px;padding:28px 34px;background:{WEEKLY_BG};page-break-after:always;overflow:hidden}} .page:last-child{{page-break-after:auto}}
      .top{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:5px solid {WEEKLY_GOLD};padding-bottom:12px;margin-bottom:12px}}
      .kicker{{font-size:12px;font-weight:900;color:#b8871b;letter-spacing:.12em}} h1{{font-size:32px;line-height:1.08;margin:4px 0 0;color:{WEEKLY_DARK}}}
      .date{{font-size:14px;color:#6e746f;margin-top:5px}} .close{{text-align:right}} .close span{{display:block;color:#6e746f;font-size:13px}} .close b{{font-size:34px;line-height:1.05}} .close em{{display:block;font-style:normal;font-size:21px;font-weight:900}}
      .panel{{background:#fffdf7;border:1px solid #ded4b8;border-radius:13px;padding:13px 15px;margin-bottom:11px}}
      .market-hero{{display:grid;grid-template-columns:190px 1fr;gap:16px;align-items:stretch}} .status{{font-size:34px;font-weight:900;color:{market_weekly.get('posture_color', WEEKLY_DARK)};line-height:1.05}} .summary{{font-size:16px;line-height:1.48;color:#31423a}} .note{{font-size:12px;color:#59665f;background:#f7efdc;border-left:5px solid {WEEKLY_GOLD};padding:7px 9px;margin-top:7px}} .hero-note{{margin-top:0;line-height:1.55}} .hero-note b{{display:block;font-size:15px;color:{WEEKLY_DARK};margin-bottom:4px}}
      .chart-wrap{{padding:8px 10px 6px}} .chart-wrap svg{{display:block;max-width:100%;height:auto}} .page1-grid{{display:grid;grid-template-columns:1fr 1fr;gap:11px}}
      .metric-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:9px}} .metric{{background:#f4edd9;border-radius:11px;padding:10px 11px;min-height:104px}} .metric span{{font-size:12px;color:#747d77}} .metric b{{display:block;font-size:21px;margin-top:2px}} .metric p{{font-size:11px;line-height:1.32;color:#5f6b64;margin:3px 0 0}}
      h2{{font-size:22px;margin:0 0 8px;color:{WEEKLY_DARK}}} .twocol{{display:grid;grid-template-columns:1fr 1fr;gap:9px}} .page1-grid .twocol{{grid-template-columns:1fr;gap:7px}}
      .mini{{background:#fbf7eb;border-radius:9px;padding:8px 10px;min-height:67px;border-left:5px solid #d4bf7a}} .event-mini{{border-left-color:var(--c)}} .mini b{{display:block;font-size:13px;line-height:1.25}} .mini span{{display:block;font-size:11px;color:#59665f;line-height:1.3;margin-top:4px}}
      .rank-row{{display:grid;grid-template-columns:126px 1fr 72px;gap:10px;align-items:center;margin:8px 0}} .rank-name{{font-size:15px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}} .rank-track{{height:17px;background:#e8dfc8;border-radius:999px;overflow:hidden}} .rank-fill{{height:17px;width:var(--w);background:var(--c);border-radius:999px}} .rank-val{{font-size:16px;font-weight:900;text-align:right}}
      .stock-grid{{display:grid;grid-template-columns:1fr 1fr;gap:9px}} .stock-card{{background:#fffdf7;border:1px solid #ded4b8;border-left:7px solid var(--c);border-radius:11px;padding:10px 11px;min-height:120px}} .stock-head{{display:flex;justify-content:space-between;gap:8px}} .stock-head b{{font-size:18px}} .stock-head span{{display:block;font-size:11px;color:#758078;margin-top:1px}} .signal-dot{{width:24px;height:24px;border-radius:50%;background:var(--c);border:3px solid #fff;box-shadow:0 0 0 1px rgba(18,50,43,.18),0 2px 7px rgba(18,50,43,.20),inset 0 -2px 3px rgba(0,0,0,.12);flex:0 0 auto;position:relative}} .signal-dot span{{position:absolute;left:4px;top:4px;width:7px;height:7px;border-radius:50%;background:rgba(255,255,255,.92);box-shadow:0 0 4px rgba(255,255,255,.75)}} .stock-line{{display:flex;justify-content:space-between;margin-top:6px;font-size:14px;font-weight:900}} .stock-action{{font-size:12px;color:#4d5c55;margin-top:5px}} .stock-card p{{font-size:11px;line-height:1.32;color:#59665f;margin:5px 0 0}}
      .detail-card{{background:#fffdf7;border:1px solid #ded4b8;border-left:8px solid var(--c);border-radius:12px;padding:11px 13px;margin-bottom:10px;min-height:262px}} .detail-top{{display:flex;justify-content:space-between;gap:12px}} .detail-top b{{font-size:21px}} .detail-top span{{display:block;color:#758078;font-size:11px;margin-top:2px}} .detail-top strong{{display:block;font-size:21px;text-align:right}} .detail-top em{{display:block;font-style:normal;text-align:right;font-weight:900;font-size:14px}} .detail-reason{{font-size:12px;line-height:1.4;color:#4d5c55;margin:7px 0 8px}}
      .radar-grid{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px}} .radar-tile{{background:#f4edd9;border-radius:8px;padding:7px;min-height:68px}} .radar-label{{font-size:10px;color:#747d77}} .radar-value{{font-size:12px;font-weight:900;margin-top:2px;line-height:1.2}} .radar-note{{font-size:9px;color:#66736b;line-height:1.22;margin-top:3px}}
      .footer{{font-size:11px;text-align:center;color:#8a806b;margin-top:7px}}
      @media screen {{
        body{{overflow-x:hidden}}
        .page{{width:100vw;max-width:900px;height:auto;min-height:1260px;padding:28px clamp(18px,3.8vw,34px)}}
      }}
      @media screen and (max-width:760px) {{
        .top{{gap:14px}} .close b{{font-size:26px}} .close em{{font-size:17px}}
        .market-hero{{grid-template-columns:1fr;gap:10px}} .status{{font-size:30px}}
        .metric-grid,.stock-grid,.page1-grid{{grid-template-columns:1fr}}
        .radar-grid{{grid-template-columns:1fr 1fr}}
      }}
    </style>
    """

    page1 = f"""
    <div class='page'>
      <div class='top'><div><div class='kicker'>WEEKLY MARKET BRIEF</div><h1>每週台股報告</h1><div class='date'>{date_text}｜{meta['week_label']}｜免費摘要版</div></div><div class='close'><span>加權指數收盤</span><b>{market.get('close',0):.2f}</b><em style='color:{_pct_color(market_weekly.get('week_chg_pct'))}'>{pct_text(market_weekly.get('week_chg_pct'))}</em></div></div>
      <div class='panel market-hero'><div class='status'>{html_lib.escape(market_weekly.get('posture','觀察'))}</div><div class='note hero-note'><b>{html_lib.escape(market_weekly.get('trend_summary',''))}</b>{html_lib.escape(market_weekly.get('next_focus',''))}｜週報偏向中大型權值股與中長線條件觀察，不作為具體交易建議。口徑：本週漲跌為週一開盤至週五收盤；相對上週五用於觀察跳空與週線連續性。</div></div>
      <div class='panel chart-wrap'>{market_chart}</div>
      <div class='panel'><h2>宏觀指標</h2><div class='metric-grid'><div class='metric'><span>法人週累計（金額）</span><b style='color:{_pct_color(market_weekly.get('institutional_value'))}'>{inst_value_text}</b>{render_sparkline(inst_series, 170, 34)}<p>{html_lib.escape(_social_short_text(inst_note, 62))}</p></div><div class='metric'><span>美元/台幣</span><b>{fx_value}</b>{render_sparkline(fx_series, 170, 34)}<p>{html_lib.escape(_social_short_text(fx_note, 62))}</p></div><div class='metric'><span>美10年債</span><b>{rates_value}</b>{render_sparkline(rates_series, 170, 34)}<p>{html_lib.escape(_social_short_text(rates_note, 62))}</p></div></div></div>
      <div class='page1-grid'><div class='panel'><h2>重大事件</h2><div class='twocol'>{_public_event_items(event_items, today, 4)}</div></div>
      <div class='panel'><h2>重點新聞</h2><div class='twocol'>{_public_news_items(news_items, 4)}</div></div></div>
      <div class='footer'>本報告由自動化模型產生，僅供參考，不構成投資建議。</div>
    </div>"""
    page2 = f"""
    <div class='page'>
      <div class='top'><div><div class='kicker'>LARGE CAP MAP</div><h1>權值股總覽</h1><div class='date'>{date_text}｜依本週漲跌排序</div></div></div>
      <div class='panel'><h2>本週漲跌排名</h2>{ranking}</div>
      <div class='panel'><h2>8 檔追蹤標的</h2><div class='stock-grid'>{stock_cards}</div></div>
      <div class='footer'>信號燈為週報趨勢分層，正向條件與風險條件分數只代表模型觀察結果。</div>
    </div>"""
    page3 = f"""
    <div class='page'>
      <div class='top'><div><div class='kicker'>KEY INDICATORS</div><h1>市場與強勢標的雷達</h1><div class='date'>{date_text}｜季線、BIAS60、法人、動能</div></div></div>
      {detail_pages[0] if detail_pages else ''}
      <div class='footer'>跌深反彈不等於趨勢反轉；週報以安全邊際與趨勢修復作為主要判斷。</div>
    </div>"""
    page4 = f"""
    <div class='page'>
      <div class='top'><div><div class='kicker'>KEY INDICATORS</div><h1>修正與觀察標的雷達</h1><div class='date'>{date_text}｜量能、OBV、KD、MACD</div></div></div>
      {detail_pages[1] if len(detail_pages) > 1 else ''}
      <div class='footer'>同一弱訊號連續出現時，不代表狀態改變；只有訊號升級或條件改變再重新評估。</div>
    </div>"""
    return f"<!DOCTYPE html><html><head><meta charset='utf-8'>{css}</head><body>{page1}{page2}{page3}{page4}</body></html>"

# ── 本機 HTML 預覽 ───────────────────────────────────────────
def save_email_preview(html: str) -> Path:
    preview_path = Path(__file__).parent / "email_preview.html"
    preview_path.write_text(html, encoding="utf-8")
    return preview_path


# ── 產生分享圖片與上傳雲端硬碟 ───────────────────────────────
def render_report_image(html_path: Path, today: str, cfg: dict, output_name: str | None = None, full_page: bool = True, height: int = 1200) -> Path | None:
    drive_cfg = cfg.get("drive_report", {})
    if not drive_cfg.get("enabled", False):
        return None

    image_path = Path(__file__).parent / (output_name or f"{today.replace('-', '')}.png")
    width = int(drive_cfg.get("image_width", 900))

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"⚠️  未安裝 Playwright，跳過產生圖片：{exc}")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=2,
            )
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.screenshot(path=str(image_path), full_page=full_page)
            browser.close()
        return image_path
    except Exception as exc:
        print(f"⚠️  產生報告圖片失敗：{exc}")
        return None


def render_report_pdf(html_path: Path, output_name: str, prefer_css_page_size: bool = False) -> Path | None:
    pdf_path = Path(__file__).parent / output_name
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"⚠️  未安裝 Playwright，跳過產生 PDF：{exc}")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 900, "height": 1260}, device_scale_factor=1)
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            pdf_options = {
                "path": str(pdf_path),
                "print_background": True,
                "prefer_css_page_size": prefer_css_page_size,
            }
            if not prefer_css_page_size:
                pdf_options.update({
                    "width": "900px",
                    "margin": {"top": "0", "right": "0", "bottom": "0", "left": "0"},
                })
            page.pdf(**pdf_options)
            browser.close()
        return pdf_path
    except Exception as exc:
        print(f"⚠️  產生 PDF 失敗：{exc}")
        return None


def get_drive_target_folder_id(service, cfg: dict, report_meta: dict, create: bool = False) -> str | None:
    drive_cfg = cfg.get("drive_report", {})
    folder_id = resolve_backup_drive_folder_id(drive_cfg)
    if not folder_id:
        return None

    for raw_name in drive_cfg.get("folder_path", []):
        name = str(raw_name).format(**report_meta)
        query = (
            f"'{folder_id}' in parents and "
            f"name = '{drive_name_query(name)}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        existing = service.files().list(
            q=query,
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute().get("files", [])
        if existing:
            folder_id = existing[0]["id"]
            continue
        if not create:
            return None
        folder = service.files().create(
            body={
                "name": name,
                "parents": [folder_id],
                "mimeType": "application/vnd.google-apps.folder",
            },
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        folder_id = folder["id"]
    return folder_id


def drive_file_exists(file_name: str, cfg: dict) -> bool:
    drive_cfg = cfg.get("drive_report", {})
    if not drive_cfg.get("enabled", False):
        return False

    service, _auth_mode = build_google_drive_service()
    if not service:
        print("⚠️  無法檢查 Google Drive 既有檔案，繼續執行避免漏寄")
        return False
    date_match = re.search(r"(20\d{6})", file_name)
    if date_match:
        report_dt = datetime.strptime(date_match.group(1), "%Y%m%d").replace(tzinfo=TAIPEI_TZ)
    else:
        report_dt = datetime.now(TAIPEI_TZ)
    report_meta = get_report_meta(report_dt)
    folder_id = get_drive_target_folder_id(service, cfg, report_meta, create=False)
    if not folder_id:
        return False

    try:
        query = (
            f"'{folder_id}' in parents and "
            f"name = '{drive_name_query(file_name)}' and "
            "trashed = false"
        )
        existing = service.files().list(
            q=query,
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute().get("files", [])
        if existing:
            print(f"Google Drive 已有 {file_name}，視為本週完整發布完成，跳過本次每小時重試")
            return True
    except Exception as exc:
        print(f"⚠️  檢查 Google Drive 既有檔案失敗，繼續執行避免漏寄：{exc}")
    return False

def upload_report_image_to_drive(image_path: Path, today: str, cfg: dict) -> str | None:
    drive_cfg = cfg.get("drive_report", {})
    if not drive_cfg.get("enabled", False):
        return None

    try:
        from googleapiclient.http import MediaFileUpload
    except Exception as exc:
        print(f"⚠️  未安裝 Google Drive API 套件，跳過上傳：{exc}")
        return None

    service, auth_mode = build_google_drive_service()
    if not service:
        print("⚠️  未設定 Google OAuth 憑證，已保留本機圖片但跳過上傳")
        return None
    report_meta = get_report_meta(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ))
    folder_id = get_drive_target_folder_id(service, cfg, report_meta, create=True)
    if not folder_id:
        print("⚠️  未設定 Google Drive folder_id，跳過上傳圖片")
        return None

    try:
        print(f"使用 Google Drive {auth_mode} 憑證上傳圖片")
        file_name = image_path.name
        media = MediaFileUpload(str(image_path), mimetype="image/png", resumable=False)
        query = (
            f"'{folder_id}' in parents and "
            f"name = '{drive_name_query(file_name)}' and "
            "trashed = false"
        )
        existing = service.files().list(
            q=query,
            fields="files(id,name,webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute().get("files", [])

        if existing:
            uploaded = service.files().update(
                fileId=existing[0]["id"],
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()
        else:
            uploaded = service.files().create(
                body={"name": file_name, "parents": [folder_id]},
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()

        return uploaded.get("webViewLink")
    except Exception as exc:
        print(f"⚠️  上傳 Google Drive 失敗：{exc}")
        return None
def upload_report_file_to_drive(file_path: Path, today: str, cfg: dict,
                                file_name: str | None = None,
                                mime_type: str = "application/pdf") -> str | None:
    drive_cfg = cfg.get("drive_report", {})
    if not drive_cfg.get("enabled", False):
        return None
    service, _auth_mode = build_google_drive_service()
    if not service:
        print("⚠️  未設定 Google OAuth 憑證，已保留本機備份 PDF 但跳過上傳")
        return None
    report_meta = get_report_meta(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ))
    folder_id = get_drive_target_folder_id(service, cfg, report_meta, create=True)
    if not folder_id:
        print("⚠️  未設定 Google Drive 備份 folder_id，跳過上傳備份 PDF")
        return None
    uploaded = upload_file_to_drive(file_path, folder_id, mime_type, file_name=file_name or file_path.name)
    return uploaded.get("webViewLink") if uploaded else None


def save_public_report_file(html: str, today: str, cfg: dict) -> Path | None:
    public_cfg = cfg.get("public_report", {})
    if not public_cfg.get("enabled", False):
        return None
    out_dir = Path(__file__).parent / "public_report"
    out_dir.mkdir(exist_ok=True)
    html_path = out_dir / "public_report_preview.html"
    html_path.write_text(html, encoding="utf-8")
    fixed_name = public_cfg.get("fixed_file_name", "每週台股報告.pdf")
    pdf_path = render_report_pdf(html_path, str(Path("public_report") / fixed_name), prefer_css_page_size=True)
    if pdf_path:
        print(f"已產生免費版 PDF：{pdf_path}")
    return pdf_path


def upload_public_report_file(file_path: Path | None, cfg: dict) -> str | None:
    if not file_path:
        return None
    public_cfg = cfg.get("public_report", {})
    if not public_cfg.get("enabled", False):
        return None
    folder_id = resolve_public_report_folder_id(public_cfg)
    if not folder_id:
        print("⚠️  未設定免費版 Google Drive folder_id，跳過上傳固定 PDF")
        return None
    file_id = resolve_public_report_file_id(public_cfg)
    uploaded = upload_file_to_drive(
        file_path,
        folder_id,
        "application/pdf",
        file_name=public_cfg.get("fixed_file_name", "每週台股報告.pdf"),
        make_public=bool(public_cfg.get("make_public", True)),
        file_id=file_id or None,
    )
    return uploaded.get("webViewLink") if uploaded else None


def validate_complete_report_results(results: list, watchlist: list, expected_date: str) -> None:
    expected = {stock["ticker"]: stock.get("name", stock["ticker"]) for stock in watchlist}
    actual = {}
    duplicates = []
    stale = []

    for name, ticker, result in results:
        if ticker in actual:
            duplicates.append(ticker)
        actual[ticker] = result
        data_date = result.get("data_date")
        if data_date != expected_date:
            stale.append(f"{name}({ticker})={data_date or '無資料日'}")

    missing = [f"{name}({ticker})" for ticker, name in expected.items() if ticker not in actual]
    issues = []
    if missing:
        issues.append(f"缺少 {', '.join(missing)}")
    if stale:
        issues.append(f"資料日不符 {', '.join(stale)}")
    if duplicates:
        issues.append(f"重複標的 {', '.join(sorted(set(duplicates)))}")

    if issues:
        raise RuntimeError(f"週報完整性檢查失敗：{'；'.join(issues)}")


def _write_github_output(name: str, value: str) -> None:
    write_github_output(name, value)


def run_schedule_gate() -> None:
    cfg = load_config()
    now_tw = datetime.now(TAIPEI_TZ)
    force_run = env_flag("FORCE_RUN_REPORT")
    target_date = resolve_report_target(now_tw, force_run)
    backup_pdf_name = f"每週台股報告_{target_date.strftime('%Y%m%d')}.pdf"
    should_run = force_run or not drive_file_exists(backup_pdf_name, cfg)
    _write_github_output("target_date", target_date.strftime("%Y-%m-%d"))
    _write_github_output("should_run", "true" if should_run else "false")
    if should_run:
        print(f"週報尚未完整發布：{backup_pdf_name}，執行完整產報流程")
    else:
        print(f"週報已完整發布：{backup_pdf_name}，本次排程在閘門停止")


# ── 主流程 ───────────────────────────────────────────────────
def main():
    cfg   = load_config()
    now_tw = datetime.now(TAIPEI_TZ)
    force_run = env_flag("FORCE_RUN_REPORT")
    target_date = resolve_report_target(now_tw, force_run)
    expected_date = target_date.strftime("%Y-%m-%d")
    target_dt = datetime.combine(target_date, WEEKLY_REPORT_START_TIME, tzinfo=TAIPEI_TZ)
    report_meta = get_report_meta(target_dt)
    today = report_meta["date"]
    print(
        f"[{now_tw.strftime('%Y-%m-%d %H:%M')}] 週報目標交易日={expected_date}，"
        f"開始每週趨勢分析，共 {len(cfg['watchlist'])} 檔"
    )
    if in_acceptance_drive_mode():
        print("  驗收發布模式：固定 PDF 與日期備份將上傳至測試資料夾，不使用正式固定 file_id")
    if force_run:
        print("  force_run=true，忽略同週既有檔案檢查，強制重新產生並更新 PDF")
    backup_pdf_name = f"每週台股報告_{report_meta['date_key']}.pdf"
    if not force_run and drive_file_exists(backup_pdf_name, cfg):
        return

    macro = fetch_market_context()
    if macro.get("fx"):
        print(f"  總體環境：美元/台幣 {macro['fx']['value']:.3f}", end="")
    if macro.get("rates"):
        print(f"｜美10年債 {macro['rates']['value']:.2f}%", end="")
    if macro.get("fx") or macro.get("rates"):
        print()
    elif macro.get("errors"):
        print(f"  總體環境資料暫不可用：{'；'.join(macro['errors'])}")

    news_items = fetch_auto_news(cfg)
    print(f"  自動新聞掃描：取得 {len(news_items)} 則高關聯新聞")

    market_inst_value_week = fetch_market_institutional_value_week(target_dt)

    results = []
    for stock in cfg["watchlist"]:
        ticker = stock["ticker"]
        name   = stock["name"]
        note   = stock.get("note", "")
        print(f"  {name} ({ticker}) ...", end=" ")
        try:
            scfg = get_stock_cfg(stock, cfg)
            df   = fetch_data(ticker, cfg["lookback_days"], target_date)
            data_date = df.index[-1].strftime("%Y-%m-%d")
            data_dt = datetime.strptime(data_date, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ)
            df   = calc_indicators(df, scfg)
            inst = fetch_institutional(ticker) if scfg.get("use_institutional", True) else None
            inst_week = fetch_weekly_institutional(ticker, data_dt) if scfg.get("use_institutional", True) else None
            r    = evaluate_weighted(df, scfg, inst, macro, inst_week)
            if ticker == "^TWII" and market_inst_value_week.get("success"):
                r["weekly"]["institutional_value"] = market_inst_value_week.get("total")
                r["weekly"]["institutional_value_text"] = format_twd_billion_short(market_inst_value_week.get("total"))
                r["weekly"]["institutional_daily_values"] = _cumulative([x.get("total", 0) for x in market_inst_value_week.get("daily", [])])
                r["weekly"]["institutional_week_value"] = market_inst_value_week
            r["stock_note"] = note
            r["data_date"] = data_date
            results.append((name, ticker, r))
            print(
                f"{r['emoji']} {r['summary']} | "
                f"資料日={data_date} | "
                f"週數={report_meta['week']} | "
                f"有效買{r['effective_buy']:.0f}/賣{r['effective_sell']:.0f} "
                f"(原始買{r['buy_score']:.0f}/賣{r['sell_score']:.0f}) | "
                f"BIAS60={r['b60']['bias60']:.1f}%"
            )
        except Exception as e:
            print(f"❌ {e}")

    validate_complete_report_results(results, cfg["watchlist"], expected_date)
    print(
        f"✅ 週報完整性檢查通過：{len(results)}/{len(cfg['watchlist'])} 檔，"
        f"資料日={expected_date}"
    )

    event_items = fetch_auto_market_events(cfg, today)
    print(f"  自動重大事件掃描：取得 {len(event_items)} 則高關聯事件")

    backup_pdf_name = f"每週台股報告_{report_meta['date_key']}.pdf"
    if not force_run and drive_file_exists(backup_pdf_name, cfg):
        return

    html = build_email_html(results, today, cfg, macro, news_items, event_items)
    preview_path = save_email_preview(html)
    print(f"\n已產生 Email 預覽：{preview_path}")

    if cfg.get("email", {}).get("enabled", False):
        print("\nEmail 設定啟用，準備發送 ...")
        try:
            if send_report_email(cfg, html, today, report_meta):
                print("✅ Email 發送成功")
        except Exception as e:
            print(f"❌ Email 失敗：{e}")
    else:
        print("\nEmail 發送已關閉，本週報告不依賴 SMTP secrets")

    public_html = build_public_report_html(results, today, cfg, macro, news_items, event_items)
    public_pdf_path = save_public_report_file(public_html, today, cfg)
    public_link = upload_public_report_file(public_pdf_path, cfg)
    if public_link:
        print(f"已上傳或更新免費版固定 PDF：{public_link}")
    elif email_disabled(cfg) and cfg.get("public_report", {}).get("enabled", False):
        handle_drive_publish_failure(
            cfg,
            "Email 已關閉，但免費觀眾 Google Drive PDF 上傳失敗，發布流程中止",
        )

    public_preview_path = Path(__file__).parent / "public_report" / "public_report_preview.html"
    backup_pdf_path = render_report_pdf(public_preview_path, backup_pdf_name, prefer_css_page_size=True)
    backup_link = None
    if backup_pdf_path:
        print(f"已產生自用備份 PDF：{backup_pdf_path}")
        backup_link = upload_report_file_to_drive(
            backup_pdf_path,
            today,
            cfg,
            file_name=backup_pdf_name,
            mime_type="application/pdf",
        )
        if backup_link:
            print(f"已上傳或更新自用備份 PDF：{backup_link}")
        elif email_disabled(cfg) and cfg.get("drive_report", {}).get("enabled", False):
            handle_drive_publish_failure(
                cfg,
                "Email 已關閉，但自用備份 Google Drive PDF 上傳失敗，發布流程中止",
            )
    elif email_disabled(cfg) and cfg.get("drive_report", {}).get("enabled", False):
        handle_drive_publish_failure(
            cfg,
            "Email 已關閉，但自用備份 PDF 產生失敗，發布流程中止",
        )

    if public_link and backup_link:
        print("✅ 免費固定 PDF 與自用備份 PDF 已完整上傳 Google Drive；後續每小時排程將自動跳過")



if __name__ == "__main__":
    if "--schedule-gate" in sys.argv:
        run_schedule_gate()
    else:
        main()

