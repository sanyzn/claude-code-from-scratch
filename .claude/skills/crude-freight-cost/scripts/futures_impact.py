#!/usr/bin/env python3
"""运输成本对原油期货价格的影响：占比、趋势、传导分解（仅标准库）。

输入 CSV（UTF-8，首行表头；缺列/空值允许，相应指标自动跳过）：
  date            日期 YYYY-MM-DD（必需）
  freight         即期运费 $/bbl（或给 ws+flat 由脚本换算；flat 缺省 20.21，api 缺省 33）
  ws, flat        WS 点与平准费率 $/t
  fwd_freight     远期/预期运费 $/bbl（FFA、期租反推；期货对应交割月时应优先看它）
  war             战争险等附加 $/bbl
  sc              SC 期货 元/桶
  fx              USD/CNY
  brent           Brent 期货 $/bbl
  dubai           Dubai/Oman（FOB 口径）$/bbl
  sc_brent        直接给出的 SC−Brent $/bbl（有来源报价时优先于 sc/fx/brent 推算）
  其他列（如 src、note）原样忽略，可用来记录出处

输出（markdown）：
  1. 逐行：SC 折美元、SC−Brent、运费占 SC / 占 Brent 比例、阶段（常态/偏高/危机）
  2. 趋势：区间均值、最新值所处分位、近 N 个点的斜率（每 30 天）、运费与占比的方向
  3. 传导：Δ(SC−Brent) 对 Δ运费 的回归 β（=多少运费变化进了内外价差），
           若有 dubai：Δ(Dubai−Brent) 对 Δ运费 的回归（=产地卖方吸收的部分），残差=国内因素
  4. 即期 vs 远期：分别用 freight、fwd_freight 计算“理论 SC−Dubai”，看盘面更贴近哪一个

用法：python3 futures_impact.py data.csv [--window 6] [--json]
"""
import argparse
import csv
import json
import math
import sys
from datetime import date

BBL_PER_T_API33 = 7.3194


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def load(path):
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            d = {k.strip(): _f(v) for k, v in r.items() if k and k.strip() != "date"}
            d["date"] = r["date"].strip()
            if d.get("freight") is None and d.get("ws") is not None:
                flat = d.get("flat") or 20.21
                bpt = BBL_PER_T_API33 if d.get("api") is None else 1000 / (141.5 / (131.5 + d["api"]) * 158.831)
                d["freight"] = d["ws"] / 100 * flat / bpt
            rows.append(d)
    rows.sort(key=lambda x: x["date"])
    return rows


def regime(share):
    if share is None:
        return ""
    return "常态" if share < 0.05 else ("偏高" if share < 0.15 else "危机")


def ols(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((p[1] - my) ** 2 for p in pts)
    ss_res = sum((p[1] - a - b * p[0]) ** 2 for p in pts)
    r2 = 1 - ss_res / ss_tot if ss_tot else None
    se = math.sqrt(ss_res / (n - 2) / sxx) if n > 2 else None
    return dict(beta=b, alpha=a, r2=r2, n=n, se=se)


def days(d):
    y, m, dd = map(int, d[:10].split("-"))
    return date(y, m, dd).toordinal()


def percentile_rank(vals, v):
    vals = [x for x in vals if x is not None]
    if not vals or v is None:
        return None
    return sum(1 for x in vals if x <= v) / len(vals) * 100


def analyze(rows, window):
    for r in rows:
        sc, fx, brent = r.get("sc"), r.get("fx"), r.get("brent")
        r["sc_usd"] = sc / fx if sc and fx else None
        if r.get("sc_brent") is None:
            r["sc_brent"] = r["sc_usd"] - brent if r["sc_usd"] is not None and brent else None
        tot = (r.get("freight") or 0) + (r.get("war") or 0) if r.get("freight") is not None else None
        r["transport"] = tot
        r["share_sc"] = tot / r["sc_usd"] if tot is not None and r["sc_usd"] else None
        r["share_brent"] = tot / (brent + tot) if tot is not None and brent else None
        r["dubai_brent"] = r["dubai"] - brent if r.get("dubai") is not None and brent else None
        if r["sc_usd"] is not None and r.get("dubai") is not None:
            r["sc_dubai"] = r["sc_usd"] - r["dubai"]
            r["resid_spot"] = r["sc_dubai"] - tot if tot is not None else None
            fwd = r.get("fwd_freight")
            r["resid_fwd"] = r["sc_dubai"] - fwd - (r.get("war") or 0) if fwd is not None else None
    out = dict(rows=rows)

    # 趋势
    tail = [r for r in rows if r.get("freight") is not None][-window:]
    trend = {}
    if len(tail) >= 3:
        xs = [days(r["date"]) for r in tail]
        for key in ("freight", "share_brent", "share_sc", "sc_brent"):
            f = ols(xs, [r.get(key) for r in tail])
            if f:
                trend[key] = dict(per_30d=f["beta"] * 30, r2=f["r2"], n=f["n"])
    latest = rows[-1]
    out["trend"] = trend
    out["latest"] = latest
    out["pct_rank"] = {k: percentile_rank([r.get(k) for r in rows], latest.get(k))
                       for k in ("freight", "share_brent", "share_sc")}

    # 传导：差分回归
    def pass_through(key):
        # 只用两者都有值的观测，按时间顺序做一阶差分
        pts = [r for r in rows if r.get("transport") is not None and r.get(key) is not None]
        dx = [b["transport"] - a["transport"] for a, b in zip(pts, pts[1:])]
        dy = [b[key] - a[key] for a, b in zip(pts, pts[1:])]
        return ols(dx, dy)
    out["pass_sc_brent"] = pass_through("sc_brent")
    # 累计 / 边际传导率：相对第一个配对点、相对上一个配对点
    pts = [r for r in rows if r.get("transport") is not None and r.get("sc_brent") is not None]
    steps = []
    for i, r in enumerate(pts[1:], 1):
        d0 = r["transport"] - pts[0]["transport"]
        d1 = r["transport"] - pts[i - 1]["transport"]
        steps.append(dict(date=r["date"],
                          cum=(r["sc_brent"] - pts[0]["sc_brent"]) / d0 if abs(d0) > 0.5 else None,
                          marg=(r["sc_brent"] - pts[i - 1]["sc_brent"]) / d1 if abs(d1) > 0.5 else None))
    out["pass_steps"] = steps
    # SC 价格变化归因（首尾两个完整点）：ΔSC = Brent 贡献 + 价差贡献 + 汇率贡献
    full = [r for r in rows if r.get("sc") and r.get("fx") and r.get("brent") and r.get("sc_brent") is not None]
    if len(full) >= 2:
        a, b = full[0], full[-1]
        fx_mid = (a["fx"] + b["fx"]) / 2
        out["sc_attrib"] = dict(start=a["date"], end=b["date"], d_sc=b["sc"] - a["sc"],
                                brent=(b["brent"] - a["brent"]) * fx_mid,
                                spread=(b["sc_brent"] - a["sc_brent"]) * fx_mid,
                                fx=(b["fx"] - a["fx"]) * ((a["sc_usd"] + b["sc_usd"]) / 2),
                                freight=((b.get("transport") or 0) - (a.get("transport") or 0)) * fx_mid)
    out["pass_dubai_brent"] = pass_through("dubai_brent")
    out["levels_sc_brent"] = ols([r.get("transport") for r in rows], [r.get("sc_brent") for r in rows])
    if any(r.get("fwd_freight") is not None for r in rows):
        out["levels_sc_brent_fwd"] = ols([r.get("fwd_freight") for r in rows], [r.get("sc_brent") for r in rows])
    return out


def fmt(v, nd=2, pct=False):
    if v is None:
        return "—"
    return f"{v * 100:.1f}%" if pct else f"{v:,.{nd}f}"


def report(o):
    L = []
    w = L.append
    w("# 运输成本对期货价格的影响\n")
    w("## 1. 逐期明细（$/bbl）\n")
    w("| 日期 | 即期运费 | 对照运费（FFA 或期租航次成本） | 战争险 | Brent | SC(USD) | SC−Brent | Dubai−Brent | 运费占Brent到岸 | 运费占SC | 阶段 | SC−Dubai−即期运费 | SC−Dubai−对照运费 |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in o["rows"]:
        w(f"| {r['date']} | {fmt(r.get('freight'))} | {fmt(r.get('fwd_freight'))} | {fmt(r.get('war'))} | "
          f"{fmt(r.get('brent'))} | {fmt(r.get('sc_usd'))} | {fmt(r.get('sc_brent'))} | {fmt(r.get('dubai_brent'))} | "
          f"{fmt(r.get('share_brent'), pct=True)} | {fmt(r.get('share_sc'), pct=True)} | "
          f"{regime(r.get('share_brent') if r.get('share_sc') is None else r.get('share_sc'))} | "
          f"{fmt(r.get('resid_spot'))} | {fmt(r.get('resid_fwd'))} |")
    w("\n阶段划分（运费含战争险占油价）：<5% 常态，5–15% 偏高，>15% 危机。\n")

    w("## 2. 趋势\n")
    lt = o["latest"]
    pr = o["pct_rank"]
    w(f"- 最新（{lt['date']}）：运费 {fmt(lt.get('freight'))} $/bbl，处于样本 {fmt(pr['freight'], 0)} 分位；"
      f"占 Brent 到岸 {fmt(lt.get('share_brent'), pct=True)}（{fmt(pr['share_brent'], 0)} 分位）；"
      f"占 SC {fmt(lt.get('share_sc'), pct=True)}")
    names = dict(freight="运费 $/bbl", share_brent="运费占 Brent 到岸", share_sc="运费占 SC", sc_brent="SC−Brent $/bbl")
    for k, t in o["trend"].items():
        unit = "个百分点" if k.startswith("share") else " $/bbl"
        v = t["per_30d"] * (100 if k.startswith("share") else 1)
        w(f"- 近 {t['n']} 个点 {names[k]}：每 30 天 {v:+.2f}{unit}（R²={fmt(t['r2'])}）")
    w("")

    w("## 3. 传导：运费变化有多少进了期货价差\n")
    p, q, lv = o["pass_sc_brent"], o["pass_dubai_brent"], o["levels_sc_brent"]
    if p:
        w(f"- Δ(SC−Brent) = {p['beta']:.2f} × Δ运费 + {p['alpha']:.2f}（n={p['n']}，R²={fmt(p['r2'])}，β 标准误 {fmt(p['se'])}）"
          f" → 即期运费每涨 1 $/bbl，SC 相对 Brent 约多涨 {p['beta']:.2f} $/bbl")
    else:
        w("- SC−Brent 与运费的配对样本不足 3 个，无法估计传导系数")
    if o.get("pass_steps"):
        w("\n| 日期 | 累计传导率（相对首个点） | 边际传导率（相对上一个点） |\n|---|---|---|")
        for st in o["pass_steps"]:
            w(f"| {st['date']} | {fmt(st['cum'], pct=True)} | {fmt(st['marg'], pct=True)} |")
        w("\n边际传导率持续下降，说明期货已经不再跟随即期运费（市场认为高运费是脉冲，或到岸成本已超过国内能接受的上限）。\n")
    if q:
        w(f"- Δ(Dubai−Brent) = {q['beta']:.2f} × Δ运费（n={q['n']}，R²={fmt(q['r2'])}）→ 负值表示产地 FOB 价被压低，卖方吸收约 {-q['beta']:.2f}")
    if lv:
        w(f"- 水平值：SC−Brent = {lv['alpha']:.2f} + {lv['beta']:.2f} × 即期运费（n={lv['n']}，R²={fmt(lv['r2'])}）")
    lf = o.get("levels_sc_brent_fwd")
    if lf:
        w(f"- 水平值：SC−Brent = {lf['alpha']:.2f} + {lf['beta']:.2f} × 远期运费（n={lf['n']}，R²={fmt(lf['r2'])}）")
    at = o.get("sc_attrib")
    if at:
        w(f"\n**SC 价格变化归因（{at['start']}→{at['end']}，元/桶）**：ΔSC {at['d_sc']:+.1f} = Brent {at['brent']:+.1f}"
          f" + 内外价差 {at['spread']:+.1f} + 汇率 {at['fx']:+.1f}（近似，交叉项计入残差）；"
          f"同期即期运费变化折合 {at['freight']:+.1f} 元/桶，价差部分只占其 "
          f"{(at['spread'] / at['freight'] * 100) if at['freight'] else float('nan'):.0f}%")
    w("\n解读：β≈1 表示运费几乎全部由买方/到岸价承担；β 明显小于 1 时，差额要么由产地卖方吸收（看 Dubai−Brent），"
      "要么说明期货在交易远期运费而不是即期指数（看第 1 节最后两列哪一列更接近 0），剩下的是国内供需、仓单、情绪等因素。"
      "样本少于 20 个点时，只能把 β 当作方向参考。\n")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--window", type=int, default=6, help="趋势斜率使用最近 N 个点")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    o = analyze(load(a.csv), a.window)
    if a.json:
        print(json.dumps(o, ensure_ascii=False, indent=2, default=str))
    else:
        print(report(o))


if __name__ == "__main__":
    sys.exit(main())
