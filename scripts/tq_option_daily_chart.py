#!/usr/bin/env python3
"""下载指定期权/期货合约的日线数据并绘制成图。

默认合约: sc2505-p-510 -> sc2505P510
(上期能源 原油期权, sc2505 系列, 认沽, 行权价 510)

支持三个数据源:

  ine    交易所日行情文件 (免费, 无需账户, 数据最全: 含结算价/持仓量/Delta)
         https://www.ine.cn/data/tradedata/option/dailydata/kx<YYYYMMDD>.dat
         按交易日逐日抓取后筛出该合约, 交易日历取自 akshare 本地日历。
  sina   akshare 的新浪商品期权历史接口 (免费, 一次请求, 但只有 OHLC+成交量)
         ak.option_commodity_hist_sina(symbol="sc2505P510")
  tqsdk  天勤 TqSdk (数据质量最好, 但需要快期/天勤账户)

用法:
    python scripts/tq_option_daily_chart.py                      # auto: ine -> sina -> tqsdk
    python scripts/tq_option_daily_chart.py --source ine
    python scripts/tq_option_daily_chart.py --source sina
    python scripts/tq_option_daily_chart.py --source tqsdk       # 需 TQ_USER / TQ_PASSWORD
    python scripts/tq_option_daily_chart.py --from-csv out/sc2505P510_daily.csv   # 离线重画

输出:
    out/<合约>_daily.csv   日线数据
    out/<合约>_daily.png   K线 + 成交量 + 持仓量
"""

from __future__ import annotations

import argparse
import calendar as _calendar
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

UP_COLOR = "#d1493f"     # 国内习惯: 红涨
DOWN_COLOR = "#2e9e6b"   # 绿跌
OI_COLOR = "#3b6ea5"

# 交易所日行情文件。sc/lu/nr/bc 在能源中心, 其余金属/贵金属在上期所;
# 两边文件格式一致, 按顺序试即可。
EXCHANGE_DAT_HOSTS = [
    "https://www.ine.cn/data/tradedata/option/dailydata/kx{date}.dat",
    "https://www.shfe.com.cn/data/tradedata/option/dailydata/kx{date}.dat",
]
INE_PRODUCTS = {"sc", "lu", "nr", "bc"}
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.ine.cn/",
}


# --------------------------------------------------------------------------
# 合约代码
# --------------------------------------------------------------------------

def normalize_symbol(raw: str) -> str:
    """'sc2505-p-510' / 'SC2505P510' / 'INE.sc2505P510' -> 'sc2505P510'。"""
    s = raw.strip()
    if "." in s:
        s = s.split(".", 1)[1]
    compact = s.replace("-", "").replace("_", "")
    m = re.fullmatch(r"(?i)([a-z]+)(\d{3,4})([cp])(\d+)", compact)
    if m:
        product, month, cp, strike = m.groups()
        return f"{product.lower()}{month}{cp.upper()}{strike}"
    return compact


def tq_symbol(symbol: str) -> str:
    """给天勤用的带交易所前缀的代码。"""
    product = re.match(r"[a-z]+", symbol, re.I)
    product = product.group(0).lower() if product else ""
    exchange = "INE" if product in INE_PRODUCTS else "SHFE"
    return f"{exchange}.{symbol}"


def contract_window(symbol: str) -> tuple[dt.date, dt.date]:
    """由合约月份推出一个够用的抓取区间 (上市大致在到期前一年多)。"""
    m = re.search(r"(\d{3,4})", symbol)
    today = dt.date.today()
    if not m:
        return today - dt.timedelta(days=400), today
    digits = m.group(1)
    if len(digits) == 4:
        year, month = 2000 + int(digits[:2]), int(digits[2:])
    else:
        year, month = 2020 + int(digits[0]), int(digits[1:])
    last_day = _calendar.monthrange(year, month)[1]
    end = min(dt.date(year, month, last_day), today)
    return end - dt.timedelta(days=560), end


def trading_days(start: dt.date, end: dt.date) -> list[str]:
    """交易日列表。优先用 akshare 自带的本地日历 (离线可用)。"""
    try:
        from akshare.futures.cons import get_calendar

        days = get_calendar()
    except Exception:
        rng = pd.bdate_range(start, end)
        return [d.strftime("%Y%m%d") for d in rng]
    lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    return [d for d in days if lo <= d <= hi]


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------

def fetch_exchange(
    symbol: str,
    start: dt.date,
    end: dt.date,
    cache_dir: Path,
    sleep: float = 0.25,
    fill_untraded_with_settle: bool = True,
) -> pd.DataFrame:
    """逐个交易日抓交易所期权日行情文件, 筛出该合约。免费, 无需账户。"""
    import requests

    cache_dir.mkdir(parents=True, exist_ok=True)
    days = trading_days(start, end)
    print(f"[ine] 抓取 {start} ~ {end} 共 {len(days)} 个交易日 (缓存: {cache_dir})")

    rows, misses = [], 0
    for i, day in enumerate(days, 1):
        cached = cache_dir / f"kx{day}.json"
        payload = None
        if cached.exists():
            try:
                payload = json.loads(cached.read_text())
            except Exception:
                payload = None
        if payload is None:
            for template in EXCHANGE_DAT_HOSTS:
                url = template.format(date=day)
                try:
                    r = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
                    if r.status_code == 200 and r.text.strip().startswith("{"):
                        payload = r.json()
                        cached.write_text(json.dumps(payload, ensure_ascii=False))
                        break
                except Exception:
                    continue
            time.sleep(sleep)
        if payload is None:
            misses += 1
            continue

        for row in payload.get("o_curinstrument", []):
            if str(row.get("INSTRUMENTID", "")).strip() != symbol:
                continue
            rows.append(
                {
                    "datetime": pd.to_datetime(day),
                    "open": row.get("OPENPRICE"),
                    "high": row.get("HIGHESTPRICE"),
                    "low": row.get("LOWESTPRICE"),
                    "close": row.get("CLOSEPRICE"),
                    "settle": row.get("SETTLEMENTPRICE"),
                    "volume": row.get("VOLUME"),
                    "close_oi": row.get("OPENINTEREST"),
                    "delta": row.get("DELTA"),
                }
            )
            break
        if i % 40 == 0:
            print(f"  ...{i}/{len(days)} 天, 已命中 {len(rows)} 条")

    if misses:
        print(f"[ine] {misses} 个交易日没取到日行情文件 (节假日调整/文件缺失)")
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    for col in ["open", "high", "low", "close", "settle", "volume", "close_oi", "delta"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if fill_untraded_with_settle:
        # 期权无成交日 OHLC 为空/0, 用结算价补成一条平线, 保证序列连续
        untraded = df["close"].isna() | (df["close"] <= 0)
        for col in ["open", "high", "low", "close"]:
            df.loc[untraded, col] = df.loc[untraded, "settle"]
        n = int(untraded.sum())
        if n:
            print(f"[ine] {n} 个交易日无成交, 已用结算价填充 OHLC")

    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    return df.sort_values("datetime").reset_index(drop=True)


def fetch_sina(symbol: str) -> pd.DataFrame:
    """akshare 新浪商品期权历史日线。一次请求, 但没有持仓量。"""
    import akshare as ak

    print(f"[sina] ak.option_commodity_hist_sina(symbol='{symbol}')")
    raw = ak.option_commodity_hist_sina(symbol=symbol)
    df = raw.rename(columns={"date": "datetime"})
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    return df[df["close"] > 0].sort_values("datetime").reset_index(drop=True)


def fetch_tqsdk(symbol: str, days: int, user: str, password: str) -> pd.DataFrame:
    """天勤 TqSdk。需要快期/天勤账户。"""
    from tqsdk import TqApi, TqAuth

    full = tq_symbol(symbol)
    print(f"[tqsdk] get_kline_serial('{full}', 86400, {days})")
    api = TqApi(auth=TqAuth(user, password))
    try:
        df = api.get_kline_serial(full, duration_seconds=24 * 60 * 60, data_length=days).copy()
    finally:
        api.close()
    keep = ["datetime", "open", "high", "low", "close", "volume", "open_oi", "close_oi"]
    df = df[[c for c in keep if c in df.columns]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ns")
    df = df.dropna(subset=["close"])
    return df[df["close"] > 0].reset_index(drop=True)


def tq_credentials(args: argparse.Namespace) -> tuple[str, str] | None:
    user = args.user or os.environ.get("TQ_USER") or os.environ.get("TQ_ACCOUNT")
    password = args.password or os.environ.get("TQ_PASSWORD") or os.environ.get("TQ_PASS")
    return (user, password) if user and password else None


# --------------------------------------------------------------------------
# 绘图
# --------------------------------------------------------------------------

def setup_cjk_font() -> None:
    from matplotlib import font_manager

    preferred = [
        "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei",
        "Microsoft YaHei", "PingFang SC", "SimHei",
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = [n for n in preferred if n in available]
    if chosen:
        plt.rcParams["font.sans-serif"] = chosen + plt.rcParams["font.sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False


def plot_daily(df: pd.DataFrame, symbol: str, out_png: Path, source: str = "") -> Path:
    """K线 + 成交量 + 持仓量。x 轴用序号, 避免非交易日留白。"""
    if df.empty:
        raise SystemExit("没有可绘制的数据。")

    x = range(len(df))
    colors = [UP_COLOR if c >= o else DOWN_COLOR for o, c in zip(df["open"], df["close"])]

    has_oi = "close_oi" in df.columns and df["close_oi"].notna().any()
    nrows = 3 if has_oi else 2
    fig, axes = plt.subplots(
        nrows, 1,
        figsize=(13, 8 if has_oi else 6.5),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1, 1] if has_oi else [3, 1], "hspace": 0.08},
    )
    ax_price, ax_vol = axes[0], axes[1]

    for i, (_, row) in enumerate(df.iterrows()):
        color = UP_COLOR if row["close"] >= row["open"] else DOWN_COLOR
        ax_price.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.9, zorder=2)
        bottom, height = min(row["open"], row["close"]), abs(row["close"] - row["open"])
        if height == 0:
            ax_price.plot([i - 0.35, i + 0.35], [row["close"], row["close"]],
                          color=color, linewidth=1.2, zorder=3)
        else:
            ax_price.add_patch(plt.Rectangle((i - 0.35, bottom), 0.7, height,
                                             facecolor=color, edgecolor=color, zorder=3))

    first, last = df["datetime"].iloc[0], df["datetime"].iloc[-1]
    pct = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100 if df["close"].iloc[0] else float("nan")
    ax_price.set_ylabel("权利金 (元/桶)")
    ax_price.set_title(
        f"{symbol} 日线  |  {first:%Y-%m-%d} ~ {last:%Y-%m-%d}  "
        f"({len(df)} 个交易日, 末值 {df['close'].iloc[-1]:.2f}, 区间 {pct:+.1f}%)"
        + (f"   [数据源: {source}]" if source else ""),
        fontsize=13, pad=12,
    )
    ax_price.grid(alpha=0.25, linestyle="--")

    ax_vol.bar(x, df["volume"], color=colors, width=0.7)
    ax_vol.set_ylabel("成交量 (手)")
    ax_vol.grid(alpha=0.25, linestyle="--")

    if has_oi:
        ax_oi = axes[2]
        ax_oi.plot(x, df["close_oi"], color=OI_COLOR, linewidth=1.4)
        ax_oi.fill_between(x, df["close_oi"], color=OI_COLOR, alpha=0.15)
        ax_oi.set_ylabel("持仓量 (手)")
        ax_oi.grid(alpha=0.25, linestyle="--")
        ax_bottom = ax_oi
    else:
        ax_bottom = ax_vol

    step = max(1, len(df) // 12)
    ticks = list(range(0, len(df), step))
    ax_bottom.set_xticks(ticks)
    ax_bottom.set_xticklabels(
        [df["datetime"].iloc[i].strftime("%Y-%m-%d") for i in ticks], rotation=45, ha="right"
    )
    ax_bottom.set_xlim(-1, len(df))
    for ax in axes:
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _: f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}")
        )

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载合约日线数据并绘图")
    parser.add_argument("--symbol", default="sc2505-p-510", help="合约代码, 如 sc2505-p-510")
    parser.add_argument("--source", default="auto", choices=["auto", "ine", "sina", "tqsdk"],
                        help="数据源 (默认 auto: ine -> sina -> tqsdk)")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD (仅 ine 源, 默认按合约月份推算)")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD (仅 ine 源)")
    parser.add_argument("--days", type=int, default=400, help="下载多少根日线 (仅 tqsdk 源)")
    parser.add_argument("--out-dir", default="out", help="输出目录")
    parser.add_argument("--cache-dir", default=".cache/ine_daily", help="交易所日行情文件缓存目录")
    parser.add_argument("--from-csv", help="跳过下载, 直接用该 CSV 绘图")
    parser.add_argument("--user", help="天勤账户 (默认读 TQ_USER)")
    parser.add_argument("--password", help="天勤密码 (默认读 TQ_PASSWORD)")
    args = parser.parse_args(argv)

    setup_cjk_font()
    symbol = normalize_symbol(args.symbol)
    out_dir = Path(args.out_dir)
    csv_path, png_path = out_dir / f"{symbol}_daily.csv", out_dir / f"{symbol}_daily.png"

    if args.from_csv:
        df = pd.read_csv(args.from_csv)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        used = "csv"
        print(f"从 {args.from_csv} 读入 {len(df)} 根日线")
    else:
        default_start, default_end = contract_window(symbol)
        start = dt.date.fromisoformat(args.start) if args.start else default_start
        end = dt.date.fromisoformat(args.end) if args.end else default_end
        order = ["ine", "sina", "tqsdk"] if args.source == "auto" else [args.source]

        df, used, errors = pd.DataFrame(), "", []
        for src in order:
            try:
                if src == "ine":
                    df = fetch_exchange(symbol, start, end, Path(args.cache_dir))
                elif src == "sina":
                    df = fetch_sina(symbol)
                else:
                    creds = tq_credentials(args)
                    if not creds:
                        raise RuntimeError(
                            "缺少天勤账户: 请设置 TQ_USER / TQ_PASSWORD "
                            "(免费注册 https://account.shinnytech.com/)"
                        )
                    df = fetch_tqsdk(symbol, args.days, *creds)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"  {src}: {type(exc).__name__}: {exc}")
                df = pd.DataFrame()
            else:
                if df.empty:
                    errors.append(f"  {src}: 没有取到数据")
            if not df.empty:
                used = src
                break

        if df.empty:
            print("所有数据源都失败了:", *errors, sep="\n", file=sys.stderr)
            return 1

        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(csv_path, index=False)
        print(f"已保存 {len(df)} 根日线 (数据源: {used}) -> {csv_path}")

    plot_daily(df, symbol, png_path, source=used)
    print(f"已生成图表 -> {png_path}")
    cols = [c for c in ["datetime", "open", "high", "low", "close", "settle", "volume", "close_oi"]
            if c in df.columns]
    print("\n最后 5 个交易日:")
    print(df.tail(5)[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
