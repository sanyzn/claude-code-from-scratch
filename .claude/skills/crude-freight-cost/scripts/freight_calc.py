#!/usr/bin/env python3
"""原油油轮运费计算器（仅标准库）。

口径来源：
  - 货主视角：Worldscale 换算  $/t = WS/100 × 平准费率(flat rate)，$/bbl = $/t ÷ 桶/吨
  - 船东视角：波罗的海交易所 TD3C TCE 算法
        TCE = (运费毛收入 × (1-佣金) − 燃油 − 港口费 − 其他航次费) ÷ 往返总天数
        海上天数 = 航程 × (1+天气余量) ÷ (航速 × 24)
  - 盈亏平衡：船东要求的 TCE 反推货主每桶运费（Thunder Said Energy 的思路，但补上航次成本）

子命令一览（均支持 --json）：
  bpt         API 度 / 比重 → 桶/吨
  ws          WS 点 + 平准费率 → $/t、$/bbl、整船包干价
  lumpsum     整船包干价 → $/bbl、$/t
  tce         WS → TCE（波罗的海口径，参数可改）
  ws-from-tce TCE → 所需 WS 及 $/bbl（tce 的反函数）
  warrisk     战争险附加费（船价 × 费率）→ $/bbl
  landed      FOB + 运费 + 保险 + 损耗 + 港杂 → 到岸成本（USD/bbl、CNY/bbl、CNY/t）
  calibrate   用若干组公开的 (WS, TCE) 反推平准费率 / 航次成本，并做留一法回测
"""
import argparse
import json
import math
import sys

BBL_M3 = 0.158987294928  # 1 桶 = 0.158987 m³
WATER_KG_M3_60F = 999.016  # 60°F 水密度


def sg_from_api(api):
    return 141.5 / (131.5 + api)


def bbl_per_tonne(api=None, sg=None):
    """桶/吨（真空中质量近似）。API 33 的 Arab Light ≈ 7.33。"""
    if sg is None:
        if api is None:
            raise ValueError("需要 api 或 sg")
        sg = sg_from_api(api)
    return 1000.0 / (sg * WATER_KG_M3_60F * BBL_M3)


# ---------------- 默认航线规格 ----------------
# TD3C：波罗的海交易所口径（2015 年规格修订，2017 年 TCE 说明书）。
# 航程 5,900 nm 为估算值（Ras Tanura→Ningbo 经马六甲），用 calibrate 回测得到的往返天数≈46.5–46.8 天与之吻合。
ROUTES = {
    "TD3C": dict(
        desc="MEG(Ras Tanura)→China(Ningbo) 270kt VLCC",
        cargo_t=270000, dist_laden=5900, dist_ballast=5900,
        speed_laden=13.0, speed_ballast=12.5, cons_laden=70.0, cons_ballast=53.0,
        weather=0.05, load_days=2, load_cons=20, disch_days=3, disch_cons=110,
        idle_days=1, idle_cons=10, port_cost_usd=150000, commission=0.0375,
        flat_2026=20.21,  # 由上海航交所 CT1 2026-07-06 数据反推：56.98 $/t ÷ 2.8194
    ),
}


def voyage_days(p):
    sea_l = p["dist_laden"] * (1 + p["weather"]) / (p["speed_laden"] * 24)
    sea_b = p["dist_ballast"] * (1 + p["weather"]) / (p["speed_ballast"] * 24)
    port = p["load_days"] + p["disch_days"] + p["idle_days"]
    return sea_l, sea_b, port


def fuel_tonnes(p):
    sea_l, sea_b, _ = voyage_days(p)
    return (sea_l * p["cons_laden"] + sea_b * p["cons_ballast"]
            + p["load_days"] * p["load_cons"] + p["disch_days"] * p["disch_cons"]
            + p["idle_days"] * p["idle_cons"])


def tce_from_ws(ws, flat, bunker, p, extra_cost=0.0):
    sea_l, sea_b, port = voyage_days(p)
    days = sea_l + sea_b + port
    gross = p["cargo_t"] * flat * ws / 100.0
    net = gross * (1 - p["commission"])
    fuel = fuel_tonnes(p) * bunker
    costs = fuel + p["port_cost_usd"] + extra_cost
    return dict(ws=ws, flat=flat, bunker=bunker, days=days, sea_laden=sea_l, sea_ballast=sea_b,
                port_days=port, fuel_t=fuel_tonnes(p), gross_freight=gross, net_freight=net,
                fuel_cost=fuel, port_cost=p["port_cost_usd"], extra_cost=extra_cost,
                voyage_costs=costs, tce=(net - costs) / days, usd_t=flat * ws / 100.0)


def ws_from_tce(tce, flat, bunker, p, extra_cost=0.0):
    sea_l, sea_b, port = voyage_days(p)
    days = sea_l + sea_b + port
    costs = fuel_tonnes(p) * bunker + p["port_cost_usd"] + extra_cost
    gross = (tce * days + costs) / (1 - p["commission"])
    usd_t = gross / p["cargo_t"]
    return dict(tce=tce, days=days, voyage_costs=costs, gross_freight=gross,
                usd_t=usd_t, ws=usd_t / flat * 100.0 if flat else None)


def linfit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    return b, my - b * mx


def calibrate(pairs, cargo_t, commission, days_list):
    """pairs: [(ws, tce)]。TCE = s·WS − c，其中 s = cargo·flat·(1−comm)/(100·days)，c = 航次成本/天。"""
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    s, a = linfit(xs, ys)
    c = -a
    out = dict(slope_usd_day_per_ws=s, voyage_cost_per_day=c, implied_flat={})
    for d in days_list:
        out["implied_flat"][f"{d:g}d"] = s * 100 * d / (cargo_t * (1 - commission))
    loo = []
    if len(pairs) >= 3:
        for i in range(len(pairs)):
            rest = [pp for j, pp in enumerate(pairs) if j != i]
            si, ai = linfit([r[0] for r in rest], [r[1] for r in rest])
            pred = si * xs[i] + ai
            loo.append(dict(ws=xs[i], actual=ys[i], pred=pred, err_pct=(pred - ys[i]) / ys[i] * 100))
    out["leave_one_out"] = loo
    return out


def landed(fob, freight_bbl, ins_rate=0.0003, loss_rate=0.003, war_bbl=0.0,
           port_misc_bbl=0.0, fx=None, api=None, demurrage_bbl=0.0):
    """到岸成本（不含关税/增值税）。保险按 CIF×110%×费率；损耗按 (FOB+运费)×损耗率。"""
    cfr = fob + freight_bbl
    insurance = cfr * 1.1 * ins_rate / (1 - 1.1 * ins_rate)
    cif = cfr + insurance
    loss = cfr * loss_rate
    total = cif + loss + war_bbl + port_misc_bbl + demurrage_bbl
    r = dict(fob=fob, freight=freight_bbl, cfr=cfr, cargo_insurance=insurance, cif=cif,
             transit_loss=loss, war_risk=war_bbl, demurrage=demurrage_bbl, port_misc=port_misc_bbl,
             landed_usd_bbl=total, freight_share_pct=(freight_bbl + war_bbl) / total * 100)
    if fx:
        r["landed_cny_bbl"] = total * fx
        if api is not None:
            r["landed_cny_t"] = total * fx * bbl_per_tonne(api=api)
    return r


def _route_params(args):
    p = dict(ROUTES[args.route])
    for k in ("cargo_t", "dist_laden", "dist_ballast", "speed_laden", "speed_ballast",
              "cons_laden", "cons_ballast", "port_cost_usd", "commission", "weather"):
        v = getattr(args, k, None)
        if v is not None:
            p[k] = v
    return p


def _emit(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        return
    for k, v in obj.items():
        if isinstance(v, float):
            fmt = ",.4f" if abs(v) < 100 else (",.2f" if abs(v) < 1e4 else ",.0f")
            print(f"{k:>24}: {v:{fmt}}")
        else:
            print(f"{k:>24}: {v}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("bpt"); s.add_argument("--api", type=float); s.add_argument("--sg", type=float)

    s = sub.add_parser("ws")
    s.add_argument("--ws", type=float, required=True); s.add_argument("--flat", type=float, required=True)
    s.add_argument("--api", type=float, default=33.0); s.add_argument("--cargo-t", type=float, default=270000)

    s = sub.add_parser("lumpsum")
    s.add_argument("--usd", type=float, required=True)
    s.add_argument("--bbl", type=float, help="装货桶数；缺省时用 --cargo-t × 桶/吨")
    s.add_argument("--cargo-t", type=float, default=270000); s.add_argument("--api", type=float, default=33.0)

    def add_route(sp):
        sp.add_argument("--route", default="TD3C", choices=sorted(ROUTES))
        sp.add_argument("--flat", type=float, help="平准费率 $/t；缺省用路线内置值")
        sp.add_argument("--bunker", type=float, required=True, help="燃油价 $/t")
        sp.add_argument("--extra-cost", type=float, default=0.0, help="其他航次费用 USD（运河、额外战争险等）")
        for k in ("cargo_t", "dist_laden", "dist_ballast", "speed_laden", "speed_ballast",
                  "cons_laden", "cons_ballast", "port_cost_usd", "commission", "weather"):
            sp.add_argument("--" + k.replace("_", "-"), dest=k, type=float)
        sp.add_argument("--api", type=float, default=33.0)

    s = sub.add_parser("tce"); add_route(s); s.add_argument("--ws", type=float, required=True)
    s = sub.add_parser("ws-from-tce"); add_route(s); s.add_argument("--tce", type=float, required=True)

    s = sub.add_parser("warrisk")
    s.add_argument("--pct", type=float, required=True, help="战争险费率，占船价百分比")
    s.add_argument("--hull", type=float, required=True, help="船价 USD")
    s.add_argument("--bbl", type=float, default=270000 * 7.33)

    s = sub.add_parser("landed")
    s.add_argument("--fob", type=float, required=True); s.add_argument("--freight", type=float, required=True)
    s.add_argument("--ins-rate", type=float, default=0.0003); s.add_argument("--loss-rate", type=float, default=0.003)
    s.add_argument("--war", type=float, default=0.0); s.add_argument("--port-misc", type=float, default=0.0)
    s.add_argument("--demurrage", type=float, default=0.0)
    s.add_argument("--fx", type=float); s.add_argument("--api", type=float)

    s = sub.add_parser("calibrate")
    s.add_argument("--pair", action="append", required=True, help="WS:TCE，可重复")
    s.add_argument("--cargo-t", type=float, default=270000); s.add_argument("--commission", type=float, default=0.0375)
    s.add_argument("--days", type=float, action="append", help="假设往返天数，可重复")

    a = ap.parse_args(argv)

    if a.cmd == "bpt":
        r = dict(bbl_per_t=bbl_per_tonne(api=a.api, sg=a.sg))
        if a.api is not None:
            r["sg"] = sg_from_api(a.api)
    elif a.cmd == "ws":
        bpt = bbl_per_tonne(api=a.api)
        usd_t = a.flat * a.ws / 100
        r = dict(usd_t=usd_t, bbl_per_t=bpt, usd_bbl=usd_t / bpt, lumpsum_usd=usd_t * a.cargo_t)
    elif a.cmd == "lumpsum":
        bpt = bbl_per_tonne(api=a.api)
        bbl = a.bbl or a.cargo_t * bpt
        r = dict(cargo_bbl=bbl, usd_bbl=a.usd / bbl, usd_t=a.usd / bbl * bpt)
    elif a.cmd in ("tce", "ws-from-tce"):
        p = _route_params(a)
        flat = a.flat or p["flat_2026"]
        bpt = bbl_per_tonne(api=a.api)
        if a.cmd == "tce":
            r = tce_from_ws(a.ws, flat, a.bunker, p, a.extra_cost)
        else:
            r = ws_from_tce(a.tce, flat, a.bunker, p, a.extra_cost)
        r["usd_bbl"] = r["usd_t"] / bpt
    elif a.cmd == "warrisk":
        cost = a.hull * a.pct / 100
        r = dict(premium_usd=cost, usd_bbl=cost / a.bbl)
    elif a.cmd == "landed":
        r = landed(a.fob, a.freight, a.ins_rate, a.loss_rate, a.war, a.port_misc, a.fx, a.api, a.demurrage)
    elif a.cmd == "calibrate":
        pairs = [tuple(map(float, x.split(":"))) for x in a.pair]
        r = calibrate(pairs, a.cargo_t, a.commission, a.days or [44, 46.5, 49])
        if not a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
    _emit(r, a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
