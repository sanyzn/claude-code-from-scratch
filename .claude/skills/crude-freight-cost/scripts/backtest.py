#!/usr/bin/env python3
"""用公开数据回测 freight_calc.py 的口径，输出可靠性报告（markdown）。

用法：python3 backtest.py [--data ../references/backtest_data.json]
退出码：所有硬性检查通过为 0，否则为 1。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freight_calc as fc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(HERE, "..", "references", "backtest_data.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    a = ap.parse_args()
    d = json.load(open(a.data, encoding="utf-8"))
    p = dict(fc.ROUTES["TD3C"])
    sea_l, sea_b, port = fc.voyage_days(p)
    days = sea_l + sea_b + port
    fails = []
    out = []
    w = out.append

    w("# 回测结果\n")
    w(f"TD3C 默认规格：往返 {days:.2f} 天（压载 {sea_b:.2f} + 重载 {sea_l:.2f} + 港口 {port}），"
      f"燃油 {fc.fuel_tonnes(p):,.0f} 吨/航次，港口费假设 {p['port_cost_usd']:,} USD\n")

    # ---- 检验 1：WS→TCE 线性关系（同一燃油价区间，留一法）----
    pairs = [x for x in d["td3c_pairs"] if x["tier"] in ("A", "B") and x["date"] != "2026-02-20"]
    cal = fc.calibrate([(x["ws"], x["tce"]) for x in pairs], p["cargo_t"], p["commission"], [days])
    w("## 检验 1：WS 与 TCE 的线性关系（高燃油价区间 4 个点，留一法）\n")
    w(f"拟合：TCE = {cal['slope_usd_day_per_ws']:,.1f} × WS − {cal['voyage_cost_per_day']:,.0f}\n")
    w("| 日期 | WS | 公布 TCE | 留一法预测 | 误差 |\n|---|---|---|---|---|")
    for x, r in zip(pairs, cal["leave_one_out"]):
        w(f"| {x['date']} | {x['ws']} | {x['tce']:,} | {r['pred']:,.0f} | {r['err_pct']:+.2f}% |")
        if abs(r["err_pct"]) > 3:
            fails.append(f"检验1 {x['date']} 误差 {r['err_pct']:.2f}%")
    implied_flat = list(cal["implied_flat"].values())[0]
    w(f"\n由斜率反推 2026 平准费率（假设往返 {days:.1f} 天）：**{implied_flat:.2f} $/t**")
    w(f"由截距反推航次成本：{cal['voyage_cost_per_day']:,.0f} $/天 × {days:.1f} 天 = "
      f"{cal['voyage_cost_per_day'] * days / 1e6:.2f} 百万美元/航次\n")

    # 样本外：wk39（C 级来源）
    wk39 = [x for x in d["td3c_pairs"] if x["tier"] == "C"]
    for x in wk39:
        pred = cal["slope_usd_day_per_ws"] * x["ws"] - cal["voyage_cost_per_day"]
        w(f"样本外（C 级来源 {x['date']}）：WS {x['ws']} → 预测 {pred:,.0f}，来源称约 {x['tce']:,}，"
          f"差 {(pred - x['tce']) / x['tce'] * 100:+.1f}%（来源本身为约数）\n")

    # ---- 检验 2：独立来源的平准费率 ----
    s = d["sse_ct1"]
    flat_sse = s["usd_t"] / (s["ws"] / 100)
    gap = (implied_flat - flat_sse) / flat_sse * 100
    w("## 检验 2：独立来源交叉验证平准费率\n")
    w(f"上海航交所 CT1 {s['date']}：WS {s['ws']} ↔ {s['usd_t']} $/t → 平准费率 **{flat_sse:.2f} $/t**；"
      f"与检验 1 由 Baltic 数据反推的 {implied_flat:.2f} 相差 {gap:+.2f}%")
    if abs(gap) > 3:
        fails.append(f"检验2 平准费率偏差 {gap:.2f}%")
    days_needed = flat_sse * p["cargo_t"] * (1 - p["commission"]) / (100 * cal["slope_usd_day_per_ws"])
    w(f"若以 {flat_sse:.2f} 为准，Baltic 斜率隐含往返天数 {days_needed:.2f} 天（模型默认 {days:.2f} 天）\n")
    pred_ct1 = cal["slope_usd_day_per_ws"] * s["ws"] - cal["voyage_cost_per_day"]
    w(f"用 Baltic 拟合式预测 CT1 当日 TCE：{pred_ct1:,.0f}；航交所公布 {s['tce_standard']:,}（标准航速）/ "
      f"{s['tce_eco']:,}（经济航速），差 {(pred_ct1 - s['tce_standard']) / s['tce_standard'] * 100:+.1f}% / "
      f"{(pred_ct1 - s['tce_eco']) / s['tce_eco'] * 100:+.1f}%（两家船型规格不同，<5% 视为一致）\n")

    # ---- 检验 3：物理模型（燃油量×油价）----
    w("## 检验 3：Baltic 物理模型（平准费率取 CT1 反推值，燃油取当日新加坡 VLSFO）\n")
    w("| 日期 | WS | 燃油价 | 模型 TCE | 公布 TCE | 误差 |\n|---|---|---|---|---|---|")
    for x in d["td3c_pairs"]:
        if not x.get("vlsfo"):
            continue
        r = fc.tce_from_ws(x["ws"], flat_sse, x["vlsfo"], p)
        err = (r["tce"] - x["tce"]) / x["tce"] * 100
        w(f"| {x['date']} | {x['ws']} | {x['vlsfo']} | {r['tce']:,.0f} | {x['tce']:,} | {err:+.2f}% |")
        if x["tier"] != "C" and abs(err) > 5:
            fails.append(f"检验3 {x['date']} 误差 {err:.2f}%")
    feb = d["td3c_pairs"][0]
    r = fc.tce_from_ws(feb["ws"], flat_sse, feb["hsfo"], p)
    w(f"\n2026-02-20 若改用 HSFO {feb['hsfo']}：模型 TCE {r['tce']:,.0f}，误差 "
      f"{(r['tce'] - feb['tce']) / feb['tce'] * 100:+.2f}%（用于判断 Baltic 当前计价油品）\n")

    # ---- 检验 4：单位换算 ----
    w("## 检验 4：单位换算复算\n")
    w("| 说法 | 复算 | 结论 |\n|---|---|---|")
    for u in d["unit_checks"]:
        if "usd_t" in u:
            v = u["usd_t"] / u["bpt"]
        else:
            v = u["lumpsum"] / u["bbl"]
        ok = abs(v - u["claimed_usd_bbl"]) / u["claimed_usd_bbl"] < 0.03
        if not ok:
            fails.append("检验4 " + u["name"])
        w(f"| {u['name']} | {v:.2f} $/bbl | {'一致' if ok else '不一致'} |")
    w(f"\nArab Light（API 33）桶/吨 = {fc.bbl_per_tonne(api=33):.3f}；27 万吨 = "
      f"{270000 * fc.bbl_per_tonne(api=33) / 1e6:.3f} 百万桶\n")

    # ---- 检验 5：常见说法审计 ----
    bpt = fc.bbl_per_tonne(api=33)
    w("## 检验 5：文章/媒体说法审计\n")
    # a) MB Capital 8.90 $/t
    w(f"- **“TD3C 2025 WS100≈8.90 $/t”**：2026 平准费率经两条独立路径确认在 {min(flat_sse, implied_flat):.1f}–{max(flat_sse, implied_flat):.1f} $/t。"
      f"平准费率年度变化通常在 ±30% 内（2016 年燃油暴跌也只降 20–30%），8.9 意味着跌一半以上，"
      f"且 WS 50–60 的 2025 常态运价将折成 0.6–0.7 $/bbl，与“常态 1–2 $/bbl”矛盾 → **不可信**。")
    # b) 18 $/bbl
    may = fc.tce_from_ws(458.75, flat_sse, 825, p)
    base_bbl = may["usd_t"] / bpt
    tce_naive = 462102 * days / (270000 * bpt)
    w(f"- **“TD3C 46.2 万$/天 ≈ 18 $/bbl”**：WS 458.75 × {flat_sse:.2f} = {may['usd_t']:.1f} $/t = "
      f"**{base_bbl:.2f} $/bbl**（TCE×天数÷桶数的错误算法得 {tce_naive:.2f}）。"
      f"要到 18 $/bbl 需额外 {18 - base_bbl:.1f} $/bbl ≈ 每船 {(18 - base_bbl) * 270000 * bpt / 1e6:.1f} 百万美元，"
      f"与当时霍尔木兹附加战争险（船价 1.0–1.5 亿美元 × 5–10%）量级相当 → 推断该 18 美元含战争险或取自另一更高读数，"
      f"不是 WS 本身的折算，**口径不明，引用需注明**。")
    # c) TSE 2 $/bbl
    tse_naive = 100000 * 45 / 2.2e6
    req = fc.ws_from_tce(100000, flat_sse, 550, p)
    w(f"- **Thunder Said “100k$/天 ≈ 2 $/bbl”**：100,000×45 天÷220 万桶 = {tse_naive:.2f}，"
      f"即只把船东净收入折成每桶，**漏了燃油和港口费**。按 Baltic 口径、燃油 550 $/t 时，"
      f"船东要拿到 100k$/天，货主需付 {req['usd_t']:.1f} $/t = **{req['usd_t'] / bpt:.2f} $/bbl**（WS {req['ws']:.0f}）。"
      f"结论方向对（常态 2–3 $/bbl），但用于货主成本会低估约 1 $/bbl。")
    # d) TD34 WS450 → 11.5
    flat_td34 = 11.5 * bpt / 4.5
    w(f"- **“阿曼湾→中国 WS450 ≈ 11.5 $/bbl”**：隐含平准费率 {flat_td34:.2f} $/t，比 TD3C 的 {flat_sse:.2f} 低 "
      f"{(1 - flat_td34 / flat_sse) * 100:.0f}%。阿曼湾少走约 400 海里进出波斯湾（单程约 7%），方向和量级合理 → **基本可信**。")
    # e) 27-28 $/bbl
    wk36 = fc.tce_from_ws(677.22, flat_sse, 840, p)["usd_t"] / bpt
    wk39 = fc.tce_from_ws(1157.5, flat_sse, 840, p)["usd_t"] / bpt
    w(f"- **“9 月中东→中国 27–28 $/bbl，常态 1–2 $/bbl”**：按 TD3C 折算 9/3 为 {wk36:.1f}、9/24 为 {wk39:.1f} $/bbl，"
      f"27–28 落在区间内（约对应 9 月中旬 WS 1000 左右）。常态：WS 55 × {flat_sse:.1f} ÷ 7.33 = "
      f"{55 * flat_sse / 100 / bpt:.2f} $/bbl → **可信**，但必须写明对应日期。")
    oct8 = fc.ws_from_tce(1412600, flat_sse, 840, p)
    w(f"- 附：10/8 TCE 141.26 万$/天 反推 WS ≈ {oct8['ws']:.0f}，运费 ≈ {oct8['usd_t'] / bpt:.1f} $/bbl"
      f"（未计战争险）。招商轮船/国金证券称该航线成交稀少、指数失真——这个数是“指数口径”，不是实际成交价。\n")

    # ---- 检验 6：样本外 TD3C 数据点（未参与任何拟合）----
    w("## 检验 6：样本外数据点（物理模型，平准费率 20.21）\n")
    w("| 日期 | WS | 燃油价(假设) | 模型 TCE | 公布 TCE | 误差 |\n|---|---|---|---|---|---|")
    for x in d.get("td3c_out_of_sample", []):
        r = fc.tce_from_ws(x["ws"], flat_sse, x["vlsfo"], p)
        err = (r["tce"] - x["tce"]) / x["tce"] * 100
        w(f"| {x['date']} | {x['ws']} | {x['vlsfo']} | {r['tce']:,.0f} | {x['tce']:,} | {err:+.2f}% |")
        if abs(err) > 5:
            fails.append(f"检验6 {x['date']} 误差 {err:.2f}%")
    w("\n低 WS 时 TCE 对燃油价和港口费假设更敏感（WS126.5 时燃油 ±50 $/t 约影响 TCE ±3%）。\n")

    # ---- 检验 7：运费占比与期货传导 ----
    import futures_impact as fi
    rows = fi.load(os.path.join(HERE, "..", "references", "sample_2026.csv"))
    o = fi.analyze(rows, 6)
    byd = {r["date"]: r for r in o["rows"]}
    w("## 检验 7：运费占比、对期货价差的传导（sample_2026.csv）\n")
    jan, octr = byd["2026-01-15"], byd["2026-10-08"]
    w(f"- 运费占 Brent 到岸：1 月 {jan['share_brent'] * 100:.1f}%（Poten 称 3%）→ 10/8 {octr['share_brent'] * 100:.1f}%"
      f"（Poten 称 27%、Kpler 称 25%）。占 SC 价格 10/8 为 {octr['share_sc'] * 100:.1f}%。")
    poten_ok = abs(octr["share_brent"] - 0.27) < 0.03 and abs(jan["share_brent"] - 0.03) < 0.015
    if not poten_ok:
        fails.append("检验7 运费占比与 Poten 偏差超过阈值")
    w(f"- Poten 的 27% 用 33÷(33+89)=27% 可复现，说明其分母是约 89 $/bbl 的油价，不是文中提到的 120 美元。")
    lv = o["levels_sc_brent"]
    pt = o["pass_sc_brent"]
    w(f"- SC−Brent 对即期运费：水平回归 β={lv['beta']:.2f}（n={lv['n']}，R²={lv['r2']:.2f}）；差分回归 β={pt['beta']:.2f}±{pt['se']:.2f}（n={pt['n']}）。"
      f"8/27→10/8 运费涨 {octr['freight'] - byd['2026-08-27']['freight']:.1f}，SC−Brent 只涨 {octr['sc_brent'] - byd['2026-08-27']['sc_brent']:.1f}"
      f" → **即期运费只有约一半进了内外价差**，与能源研发中心“运费急升而内外价差相对平淡”的观察一致。")
    w(f"- 10/8 SC−Dubai 现货 = {octr['sc_dubai']:.2f} $/bbl：扣由 TCE 反推的即期运费估计后残差 {octr['resid_spot']:.1f}，"
      f"扣一年期租条件下的航次成本估算（{octr['fwd_freight']:.2f}）后残差 {octr['resid_fwd']:.1f}。"
      "单点观察，只作线索，不构成“期货按哪种运费定价”的结论（即期为模型估计，Dubai 取 10/7，期租不是远期运价）。")
    w("- 局限：SC/Brent/汇率配对的只有 4–5 个点，Brent 部分取月均/月末，β 仅作方向参考；用户本地有日度数据时应重跑。\n")

    w("## 结论\n")
    if fails:
        w("**未通过的硬性检查：**\n" + "\n".join(f"- {f}" for f in fails))
    else:
        w("全部硬性检查通过（检验 1 留一法误差 ≤3%，检验 2 平准费率两条路径差 ≤3%，"
          "检验 3 A/B 级数据点误差 ≤5%，检验 4 单位换算差 ≤3%，检验 6 样本外误差 ≤5%，检验 7 运费占比与 Poten 偏差 ≤3 个百分点）。检验 7 的传导和残差部分只是描述性观察，不设通过标准。")
    print("\n".join(out))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
