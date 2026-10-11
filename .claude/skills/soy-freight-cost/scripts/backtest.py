#!/usr/bin/env python3
"""检验 soy_calc.py 的口径与数据来源（输出 markdown；硬性检查失败时退出码为 1）。"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soy_calc as sc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "references", "data_2026")


def read(name):
    with open(os.path.join(DATA, name), encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def main():
    fails, out = [], []
    w = out.append
    w("# 进口大豆运输成本：检验结果\n")

    # 1. 单位换算
    bu = 2204.62262 / 60
    ok = abs(sc.BU_PER_T - bu) < 1e-3
    w(f"## 检验 1：单位换算\n\n1 吨 = 2204.62 磅 ÷ 60 磅/蒲 = {bu:.4f} 蒲；脚本取 {sc.BU_PER_T:.4f} → {'一致' if ok else '不一致'}。"
      f"100 美分/蒲 = {sc.c2t(100):.4f} 美元/吨。\n")
    if not ok:
        fails.append("检验1 单位换算")

    # 2. 完税成本公式对公开示例
    w("## 检验 2：完税成本公式对照公开示例（C 级来源，弱检验）\n")
    w("公开说法：8 月船期巴西大豆 CNF 493 美元/吨，折华南到港完税成本约 3,872 元/吨（2026-07，春秋号转引国家粮油信息中心，原文未核）。")
    w("用公式反推港杂费：港杂 = 3872 − 493 × 汇率 × 1.03 × 1.09。\n\n| 假设汇率 | 隐含港杂（元/吨） |\n|---|---|")
    implied = []
    for fx in (6.75, 6.80, 6.85, 6.90):
        p = 3872 - 493 * fx * 1.03 * 1.09
        implied.append(p)
        w(f"| {fx} | {p:.0f} |")
    ok = any(0 <= p <= 200 for p in implied)
    w(f"\n汇率在 6.75–6.90 时，隐含港杂 {min(implied):.0f}–{max(implied):.0f} 元/吨，落在常见的几十到一百多元区间 → 公式与示例{'自洽' if ok else '不自洽'}。"
      "这只能说明公式结构没有大错，不能验证具体数值。\n")
    if not ok:
        fails.append("检验2 完税成本公式")

    # 3. 运费来源一致性
    fr = read("freight_obs.csv")
    p8 = next(float(r["usd_t"]) for r in fr if r["route"].startswith("P8"))
    wp_n = next(float(r["usd_t"]) for r in fr if r["route"] == "桑托斯→中国北方")
    wp = next(float(r["usd_t"]) for r in fr if r["route"] == "桑托斯→中国")
    c3 = next(float(r["usd_t"]) for r in fr if r["route"].startswith("C3"))
    w("## 检验 3：大豆运费的两个来源是否一致\n")
    d1 = (wp_n - p8) / p8 * 100
    d2 = (wp - p8) / p8 * 100
    w(f"- Baltic P8（桑托斯→青岛，6.6 万吨，约 9/23）{p8} vs World Perspectives 同船型 {wp_n}（9/30）：差 {d1:+.1f}%")
    w(f"- 对比 World Perspectives 5.4–5.9 万吨船型 {wp}：差 {d2:+.1f}%（小船单位运费略高，方向合理）\n")
    if abs(d1) > 5:
        fails.append("检验3 P8 与 WP 偏差超过 5%")

    # 4. 货种识别：Mysteel 钢铁网的“巴西至中国”是不是大豆运费
    ms = [(r["obs_date"], float(r["usd_t"])) for r in fr if r["source_desc"].startswith("Mysteel 钢铁网")]
    w("## 检验 4：Mysteel 钢铁网“巴西至中国海运费”属于哪个货种\n")
    w("| 日期 | 数值 | 距 C3（铁矿石） | 距 P8（大豆） | 判断 |\n|---|---|---|---|---|")
    for dt, v in ms:
        dc3, dp8 = (v - c3) / c3 * 100, (v - p8) / p8 * 100
        verdict = "铁矿石口径" if abs(dc3) < abs(dp8) else "大豆口径"
        w(f"| {dt} | {v} | {dc3:+.1f}% | {dp8:+.1f}% | {verdict} |")
        if verdict != "铁矿石口径":
            fails.append(f"检验4 {dt} 货种判断与预期不符")
    w(f"\n这组数据与“西澳至中国”（铁矿石 C5 航线）并列发布，量级贴近 C3 而比大豆 P8 低约 25–30% → **不能用作大豆运费**。若误用，会把运费低估约 {sc.t2c(p8 - 42.2):.0f} 美分/蒲。\n")

    # 5. 拆分结果的合理性
    w("## 检验 5：用即期运费拆分各船期 CNF 的结果\n")
    rows = sc.curve(os.path.join(DATA, "cnf_brazil_mysteel.csv"), os.path.join(DATA, "freight_obs.csv"), "P8", 7)
    near = [r for r in rows if r["shipment"] == "2026-11" and "implied_fob" in r]
    far = [r for r in rows if r["shipment"] in ("2027-03", "2027-04", "2027-05") and "implied_fob" in r]
    w(f"- 11 月船期：隐含 FOB {min(r['implied_fob'] for r in near):.0f}–{max(r['implied_fob'] for r in near):.0f} 美分/蒲。"
      "公开渠道只查到 7 月底巴拉那瓜 FOB 约 85 美分/蒲（现货口径），11 月是巴西旧作淡季，升贴水更高符合季节规律，但**未经同期 FOB 报价验证**。")
    w(f"- 2027 年 3–5 月船期：套用即期运费后，隐含 FOB 为 {min(r['implied_fob'] for r in far):.0f} 到 {max(r['implied_fob'] for r in far):.0f} 美分/蒲（负值）。"
      "可能是新作 FOB 确实贴水，也可能是远月运价明显低于即期。**两者无法区分**，需要 P8 远期运价（FFA）或同期 FOB 报价。")
    w("- 结论：即期运费只能用来拆分最近船期；远月船期必须配远期运价。\n")

    # 6. 传导
    w("## 检验 6：传导系数能否估计\n")
    w("CNF 有 7 个日期，大豆运费只有 9/23 前后和 9/30 两个点，同日配对 2 组 → 不能估计 ΔCNF 对 Δ运费 的传导。"
      "9/23→9/30 期间运费 56.53→55.46/57.75（基本持平），11 月船期 CNF 312→315 → 这一周 CNF 的变化主要不是运费推动的（只有一个区间，仅作观察）。\n")

    w("## 结论\n")
    w("硬性检查全部通过。" if not fails else "未通过：\n" + "\n".join(f"- {f}" for f in fails))
    print("\n".join(out))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
