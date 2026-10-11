#!/usr/bin/env python3
"""进口大豆运输成本计算器（仅标准库）。

核心关系：
  1 吨大豆 = 36.7437 蒲式耳（1 蒲式耳 = 60 磅 = 27.2155 千克）
  美元/吨 = 美分/蒲式耳 × 0.367437
  CNF 升贴水 = FOB 升贴水 + 海运费（折美分/蒲式耳）
  到岸价 CNF（美元/吨）=（CBOT + CNF 升贴水）× 0.367437
  到港完税成本（元/吨）= CNF ×（1 + 保险费率）× 汇率 ×（1 + 关税）×（1 + 增值税）+ 港杂费

子命令（均支持 --json）：
  conv     美分/蒲式耳 ↔ 美元/吨
  split    拆分 CNF 升贴水：给运费求 FOB，或给 FOB 求运费
  landed   到港完税成本，以及运费占 CNF 价格、占完税成本的比例
  curve    读 Mysteel CNF 表和运费表，逐日逐船期算隐含 FOB 升贴水
  pass     ΔCNF 升贴水对 Δ运费 的回归（需要同日配对 ≥3 组）
"""
import argparse
import csv
import json
import sys
from datetime import date

KG_PER_BU = 27.2155422
BU_PER_T = 1000 / KG_PER_BU          # 36.7437
USD_T_PER_CENTS_BU = BU_PER_T / 100  # 0.367437


def c2t(cents_bu):
    return cents_bu * USD_T_PER_CENTS_BU


def t2c(usd_t):
    return usd_t / USD_T_PER_CENTS_BU


def landed(cbot, cnf_prem, fx, tariff=0.03, vat=0.09, port=0.0, ins=0.0, freight_usd_t=None):
    cnf_usd_t = c2t(cbot + cnf_prem)
    cif = cnf_usd_t * (1 + ins)
    tax_mult = (1 + tariff) * (1 + vat)
    cost = cif * fx * tax_mult + port
    r = dict(cnf_usd_t=cnf_usd_t, cif_usd_t=cif, duty_paid_cny_t=cost, tax_multiplier=tax_mult)
    if freight_usd_t is not None:
        fr_cny = freight_usd_t * (1 + ins) * fx * tax_mult  # 关税、增值税以 CIF 为基数，运费也被计税
        r.update(freight_cents_bu=t2c(freight_usd_t), freight_in_cost_cny_t=fr_cny,
                 freight_share_of_cnf=freight_usd_t / cnf_usd_t,
                 freight_share_of_premium=t2c(freight_usd_t) / cnf_prem if cnf_prem else None,
                 freight_share_of_duty_paid=fr_cny / cost)
    return r


def read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def d(s):
    y, m, dd = map(int, s[:10].split("-"))
    return date(y, m, dd)


def match_freight(fr_rows, obs_date, max_lag_days):
    """取同日或之前最近的运费观测（不向后看）。"""
    best = None
    for r in fr_rows:
        lag = (d(obs_date) - d(r["obs_date"])).days
        if 0 <= lag <= max_lag_days and (best is None or lag < best[0]):
            best = (lag, r)
    return best


def curve(cnf_path, fr_path, route_kw, max_lag):
    cnf = read(cnf_path)
    fr = [r for r in read(fr_path) if r["route"].startswith(route_kw) or (route_kw == "P8" and r["route"] == "桑托斯→中国北方")]
    out = []
    for r in cnf:
        m = match_freight(fr, r["obs_date"], max_lag)
        row = dict(obs_date=r["obs_date"], shipment=r["shipment"], cnf=float(r["cnf_cents_bu"]))
        if m:
            lag, f = m
            fc = t2c(float(f["usd_t"]))
            row.update(freight_date=f["obs_date"], lag_days=lag, freight_usd_t=float(f["usd_t"]),
                       freight_cents_bu=round(fc, 1), implied_fob=round(row["cnf"] - fc, 1),
                       caveat="远月船期用的是即期运费，未用远期曲线" if r["shipment"] > r["obs_date"][:7] else "")
        out.append(row)
    return out


def ols(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return dict(beta=b, alpha=my - b * mx, n=n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("conv"); s.add_argument("--cents-bu", type=float); s.add_argument("--usd-t", type=float)
    s = sub.add_parser("split"); s.add_argument("--cnf", type=float, required=True)
    s.add_argument("--freight-usd-t", type=float); s.add_argument("--fob", type=float)
    s = sub.add_parser("landed")
    s.add_argument("--cbot", type=float, required=True, help="CBOT 美分/蒲式耳")
    s.add_argument("--cnf", type=float, required=True, help="CNF 升贴水 美分/蒲式耳")
    s.add_argument("--fx", type=float, required=True)
    s.add_argument("--tariff", type=float, default=0.03, help="关税税率，巴西大豆最惠国 3%%；美国大豆按当时政策")
    s.add_argument("--vat", type=float, default=0.09)
    s.add_argument("--port", type=float, default=0.0, help="港杂费 元/吨（假设，需按实际）")
    s.add_argument("--ins", type=float, default=0.0, help="保险费率，如 0.001")
    s.add_argument("--freight-usd-t", type=float)
    s = sub.add_parser("curve")
    s.add_argument("--cnf-csv", required=True); s.add_argument("--freight-csv", required=True)
    s.add_argument("--route", default="P8"); s.add_argument("--max-lag", type=int, default=7)
    s = sub.add_parser("pass")
    s.add_argument("--cnf-csv", required=True); s.add_argument("--freight-csv", required=True)
    s.add_argument("--shipment", required=True); s.add_argument("--route", default="P8")
    a = ap.parse_args(argv)

    if a.cmd == "conv":
        r = {}
        if a.cents_bu is not None:
            r.update(cents_bu=a.cents_bu, usd_t=c2t(a.cents_bu))
        if a.usd_t is not None:
            r.update(usd_t_in=a.usd_t, cents_bu_out=t2c(a.usd_t))
        r["bu_per_t"] = BU_PER_T
    elif a.cmd == "split":
        if a.freight_usd_t is not None:
            fc = t2c(a.freight_usd_t)
            r = dict(cnf=a.cnf, freight_cents_bu=fc, implied_fob=a.cnf - fc, freight_share_of_premium=fc / a.cnf)
        elif a.fob is not None:
            r = dict(cnf=a.cnf, fob=a.fob, implied_freight_cents_bu=a.cnf - a.fob, implied_freight_usd_t=c2t(a.cnf - a.fob))
        else:
            ap.error("split 需要 --freight-usd-t 或 --fob")
    elif a.cmd == "landed":
        r = landed(a.cbot, a.cnf, a.fx, a.tariff, a.vat, a.port, a.ins, a.freight_usd_t)
    elif a.cmd == "curve":
        rows = curve(a.cnf_csv, a.freight_csv, a.route, a.max_lag)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print("| 观测日 | 船期 | CNF | 运费日 | 运费 $/t | 运费 ¢/bu | 隐含 FOB | 备注 |\n|---|---|---|---|---|---|---|---|")
            for x in rows:
                print(f"| {x['obs_date']} | {x['shipment']} | {x['cnf']:.0f} | {x.get('freight_date', '—')} | "
                      f"{x.get('freight_usd_t', '—')} | {x.get('freight_cents_bu', '—')} | {x.get('implied_fob', '—')} | {x.get('caveat', '无同期运费' if 'freight_date' not in x else '')} |")
        return 0
    elif a.cmd == "pass":
        cnf = {r["obs_date"]: float(r["cnf_cents_bu"]) for r in read(a.cnf_csv) if r["shipment"] == a.shipment}
        fr = {r["obs_date"]: t2c(float(r["usd_t"])) for r in read(a.freight_csv) if a.route in r["route"]}
        common = sorted(set(cnf) & set(fr))
        if len(common) < 4:
            r = dict(pairs=len(common), result="同日配对不足 4 组（差分后少于 3 组），无法估计传导系数",
                     need="与 CNF 同日的大豆运费序列（P8 或 Mysteel 农产品的巴西→中国大豆运费）")
        else:
            dx = [fr[b] - fr[a_] for a_, b in zip(common, common[1:])]
            dy = [cnf[b] - cnf[a_] for a_, b in zip(common, common[1:])]
            r = dict(pairs=len(common), **(ols(dx, dy) or {"result": "运费无变化"}))
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        for k, v in r.items():
            print(f"{k:>28}: {v:,.4f}" if isinstance(v, float) else f"{k:>28}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
