#!/usr/bin/env python3
"""从天勤 (TqSdk) 下载指定合约的日线数据并绘制成图。

默认合约: INE.sc2505P510 (上期能源 原油期权 sc2505 认沽 行权价 510)

用法:
    # 下载并绘图 (需要天勤账户)
    export TQ_USER=xxx TQ_PASSWORD=yyy
    python scripts/tq_option_daily_chart.py

    # 指定合约与天数
    python scripts/tq_option_daily_chart.py --symbol INE.sc2505C510 --days 500

    # 离线: 用已有 CSV 直接绘图 (不联网)
    python scripts/tq_option_daily_chart.py --from-csv out/sc2505P510_daily.csv

输出:
    out/<合约>_daily.csv   日线原始数据
    out/<合约>_daily.png   K线 + 成交量 + 持仓量 图
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

KLINE_COLUMNS = [
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_oi",
    "close_oi",
]

UP_COLOR = "#d1493f"     # 国内习惯: 红涨
DOWN_COLOR = "#2e9e6b"   # 绿跌
OI_COLOR = "#3b6ea5"


def setup_cjk_font() -> None:
    """让 matplotlib 能显示中文, 找不到中文字体时静默退回英文字形。"""
    from matplotlib import font_manager

    preferred = [
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "WenQuanYi Zen Hei",
        "Microsoft YaHei",
        "PingFang SC",
        "SimHei",
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = [name for name in preferred if name in available]
    if chosen:
        plt.rcParams["font.sans-serif"] = chosen + plt.rcParams["font.sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False


def normalize_symbol(raw: str) -> str:
    """把用户随手写的合约代码规整成天勤格式。

    'sc2505-p-510' / 'sc2505p510' / 'SC2505P510' -> 'INE.sc2505P510'
    已经带交易所前缀的原样返回。
    """
    s = raw.strip()
    if "." in s:
        return s
    compact = s.replace("-", "").replace("_", "")
    m = re.fullmatch(r"(?i)([a-z]+)(\d{3,4})([cp])(\d+)", compact)
    if m:
        product, month, cp, strike = m.groups()
        product = product.lower()
        exchange = {"sc": "INE", "lu": "INE", "nr": "INE", "bc": "INE"}.get(product, "SHFE")
        return f"{exchange}.{product}{month}{cp.upper()}{strike}"
    m = re.fullmatch(r"(?i)([a-z]+)(\d{3,4})", compact)
    if m:
        product, month = m.groups()
        product = product.lower()
        exchange = {"sc": "INE", "lu": "INE", "nr": "INE", "bc": "INE"}.get(product, "SHFE")
        return f"{exchange}.{product}{month}"
    return s


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    user = args.user or os.environ.get("TQ_USER") or os.environ.get("TQ_ACCOUNT")
    password = args.password or os.environ.get("TQ_PASSWORD") or os.environ.get("TQ_PASS")
    if not user or not password:
        raise SystemExit(
            "缺少天勤账户。TqSdk 3.x 下载行情必须登录快期/天勤账户 "
            "(https://account.shinnytech.com/)。\n"
            "请设置环境变量 TQ_USER / TQ_PASSWORD, 或用 --user / --password 传入。"
        )
    return user, password


def download_daily(symbol: str, days: int, user: str, password: str) -> pd.DataFrame:
    """通过天勤下载日线序列, 返回带 datetime 索引列的 DataFrame。"""
    from tqsdk import TqApi, TqAuth

    api = TqApi(auth=TqAuth(user, password))
    try:
        klines = api.get_kline_serial(symbol, duration_seconds=24 * 60 * 60, data_length=days)
        df = klines.copy()
    finally:
        api.close()

    df = df[[c for c in KLINE_COLUMNS if c in df.columns]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ns")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    return df.reset_index(drop=True)


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.dropna(subset=["close"]).reset_index(drop=True)


def plot_daily(df: pd.DataFrame, symbol: str, out_png: Path) -> Path:
    """画 K线 + 成交量 + 持仓量。x 轴用序号, 避免非交易日留白。"""
    if df.empty:
        raise SystemExit("没有可绘制的数据。")

    x = range(len(df))
    up = df["close"] >= df["open"]
    colors = [UP_COLOR if u else DOWN_COLOR for u in up]

    has_oi = "close_oi" in df.columns and df["close_oi"].notna().any()
    nrows = 3 if has_oi else 2
    height_ratios = [3, 1, 1] if has_oi else [3, 1]
    fig, axes = plt.subplots(
        nrows,
        1,
        figsize=(13, 8 if has_oi else 6.5),
        sharex=True,
        gridspec_kw={"height_ratios": height_ratios, "hspace": 0.08},
    )
    ax_price, ax_vol = axes[0], axes[1]

    # K 线: 上下影线 + 实体
    for i, (_, row) in enumerate(df.iterrows()):
        color = UP_COLOR if row["close"] >= row["open"] else DOWN_COLOR
        ax_price.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.9, zorder=2)
        bottom = min(row["open"], row["close"])
        height = abs(row["close"] - row["open"])
        if height == 0:
            ax_price.plot([i - 0.35, i + 0.35], [row["close"], row["close"]],
                          color=color, linewidth=1.2, zorder=3)
        else:
            ax_price.add_patch(
                plt.Rectangle((i - 0.35, bottom), 0.7, height,
                              facecolor=color, edgecolor=color, zorder=3)
            )

    ax_price.set_ylabel("价格 (元/桶)")
    first, last = df["datetime"].iloc[0], df["datetime"].iloc[-1]
    change = df["close"].iloc[-1] - df["close"].iloc[0]
    pct = change / df["close"].iloc[0] * 100 if df["close"].iloc[0] else float("nan")
    ax_price.set_title(
        f"{symbol} 日线  |  {first:%Y-%m-%d} ~ {last:%Y-%m-%d}  "
        f"({len(df)} 个交易日, 收盘 {df['close'].iloc[-1]:.2f}, 区间 {pct:+.1f}%)",
        fontsize=13,
        pad=12,
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

    # 日期刻度: 最多 12 个, 落在真实交易日上
    step = max(1, len(df) // 12)
    ticks = list(range(0, len(df), step))
    ax_bottom.set_xticks(ticks)
    ax_bottom.set_xticklabels(
        [df["datetime"].iloc[i].strftime("%Y-%m-%d") for i in ticks],
        rotation=45,
        ha="right",
    )
    ax_bottom.set_xlim(-1, len(df))
    for ax in axes:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}"))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从天勤下载日线数据并绘图")
    parser.add_argument("--symbol", default="sc2505-p-510", help="合约代码, 如 sc2505-p-510 或 INE.sc2505P510")
    parser.add_argument("--days", type=int, default=400, help="下载最近多少根日线 (默认 400)")
    parser.add_argument("--out-dir", default="out", help="输出目录 (默认 out)")
    parser.add_argument("--from-csv", help="跳过下载, 直接用该 CSV 绘图")
    parser.add_argument("--user", help="天勤账户 (默认读环境变量 TQ_USER)")
    parser.add_argument("--password", help="天勤密码 (默认读环境变量 TQ_PASSWORD)")
    args = parser.parse_args(argv)

    setup_cjk_font()
    symbol = normalize_symbol(args.symbol)
    out_dir = Path(args.out_dir)
    stem = symbol.split(".")[-1]
    csv_path = out_dir / f"{stem}_daily.csv"
    png_path = out_dir / f"{stem}_daily.png"

    if args.from_csv:
        df = load_csv(Path(args.from_csv))
        print(f"从 {args.from_csv} 读入 {len(df)} 根日线")
    else:
        user, password = resolve_credentials(args)
        print(f"正在从天勤下载 {symbol} 的日线数据 (最近 {args.days} 根)...")
        df = download_daily(symbol, args.days, user, password)
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(csv_path, index=False)
        print(f"已保存 {len(df)} 根日线 -> {csv_path}")

    plot_daily(df, symbol, png_path)
    print(f"已生成图表 -> {png_path}")
    if not df.empty:
        tail = df.tail(5)[["datetime", "open", "high", "low", "close", "volume", "close_oi"]]
        print("\n最后 5 个交易日:")
        print(tail.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
