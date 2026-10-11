#!/usr/bin/env python3
"""从原始观测生成运费走势数据（可复算）。

输入（references/data_2026/）：
  raw_observations.csv  每行一个公开读数：日期、序列、指标、数值、观测类型、等级、出处、链接
  price_inputs.csv      计算占比用的油价（多为月均/月末代用值，已注明）

输出（同目录，均可由本脚本重算）：
  td3c_direct.csv   TD3C 公布 WS → 每桶运费（只用平准费率和桶/吨，无模型）
  td3c_tce.csv      TD3C 公布 TCE（原单位）+ 模型估计的每桶运费及敏感性区间
  td3c_derived.csv  推算值（两周变动、月均、百分比倒推）——不是当日读数
  ct1.csv           上海航交所 CT1 美元/吨 → 每桶（7.33 桶/吨）
  td22.csv          美湾→中国包干价 → 每桶（200 万桶假设）
  period_budget.csv 1 年期租 / FFA 日租金 → “该租金条件下的航次成本估算”
  share.csv         运费÷油价、运费÷到岸成本（只用 TD3C 直接报价）
  observations.csv  原始观测加唯一 obs_id（供看板明细表使用）
  model_params.json 模型估计用到的全部参数（params.csv 为同内容的表格版）

用法：python3 build_trend_data.py [--dir ../references/data_2026]
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freight_calc as fc  # noqa: E402

FLAT_2026 = 20.21          # TD3C/CT1 平准费率，由 CT1 7/6（56.98 $/t ÷ WS281.94）反推
BPT_TD3C = round(fc.bbl_per_tonne(api=33), 4)  # 7.3194，阿拉伯轻质 API 33
BPT_CT1 = 7.33             # 国内研报惯用值
TD22_BBL = 2_000_000       # 媒体换算惯用的 200 万桶
# 当月新加坡 VLSFO 估值（Engine / Ship & Bunker 转引），仅用于 TCE→运费 的模型估计
BUNKER = {"01": 470, "02": 500, "03": 900, "04": 850, "05": 825, "06": 753,
          "07": 830, "08": 830, "09": 840, "10": 840}
DIRECT = {"周报读数", "周报中的周五评估", "转引", "指数读数", "成交（非指数）", "期租成交"}


def read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def write(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def model_bbl(tce, month, p, bunker=None, port=None):
    q = dict(p)
    if port is not None:
        q["port_cost_usd"] = port
    r = fc.ws_from_tce(tce, FLAT_2026, bunker if bunker is not None else BUNKER[month], q)
    return r["ws"], r["usd_t"] / BPT_TD3C


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "references", "data_2026"))
    a = ap.parse_args()
    raw = read(os.path.join(a.dir, "raw_observations.csv"))
    prices = read(os.path.join(a.dir, "price_inputs.csv"))
    p = dict(fc.ROUTES["TD3C"])
    sea_l, sea_b, port_days = fc.voyage_days(p)
    params = dict(
        model="本 skill 的 TD3C 航次模型（Baltic 2015 年规格 + 估算航程），未与 Baltic 现行规格文件核对",
        flat_rate_usd_t=FLAT_2026, bbl_per_t_td3c=BPT_TD3C, bbl_per_t_ct1=BPT_CT1, td22_cargo_bbl=TD22_BBL,
        cargo_t=p["cargo_t"], distance_nm_each_way=p["dist_laden"], weather_margin=p["weather"],
        speed_laden_kn=p["speed_laden"], speed_ballast_kn=p["speed_ballast"],
        cons_laden_t_day=p["cons_laden"], cons_ballast_t_day=p["cons_ballast"],
        port_days=dict(load=p["load_days"], discharge=p["disch_days"], idle=p["idle_days"]),
        port_cons_t_day=dict(load=p["load_cons"], discharge=p["disch_cons"], idle=p["idle_cons"]),
        round_trip_days=round(sea_l + sea_b + port_days, 2), fuel_t_per_voyage=round(fc.fuel_tonnes(p)),
        port_cost_usd=p["port_cost_usd"], commission=p["commission"], bunker_usd_t_by_month=BUNKER,
        sensitivity="低/高 = 燃油 −100/+100 $/t 且港口费 10 万/25 万美元",
        backtest="references/validation.md：同口径复现 Baltic 公布 TCE，A/B 级点误差 −1.5%~+1.2%",
    )
    with open(os.path.join(a.dir, "model_params.json"), "w", encoding="utf-8") as fh:
        json.dump(params, fh, ensure_ascii=False, indent=2)

    labels = dict(model="模型", flat_rate_usd_t="平准费率（美元/吨）", bbl_per_t_td3c="TD3C 桶/吨（API 33）",
                  bbl_per_t_ct1="CT1 桶/吨", td22_cargo_bbl="TD22 装货桶数（假设）", cargo_t="TD3C 船货（吨）",
                  distance_nm_each_way="单程航程（海里，估算）", weather_margin="天气余量",
                  speed_laden_kn="重载航速（节）", speed_ballast_kn="压载航速（节）",
                  cons_laden_t_day="重载油耗（吨/天）", cons_ballast_t_day="压载油耗（吨/天）",
                  port_days="港口天数", port_cons_t_day="港内油耗（吨/天）", round_trip_days="往返天数",
                  fuel_t_per_voyage="每航次燃油（吨）", port_cost_usd="两港港口费（美元，假设）",
                  commission="佣金", bunker_usd_t_by_month="当月燃油价（美元/吨，VLSFO 估值）",
                  sensitivity="敏感性区间", backtest="回测")
    write(os.path.join(a.dir, "params.csv"),
          [dict(param=labels[k], value=json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v) for k, v in params.items()])

    td3c = [r for r in raw if r["series"] == "TD3C"]
    direct, tce, derived = [], [], []
    for r in td3c:
        base = dict(obs_date=r["obs_date"], obs_type=r["obs_type"], tier=r["tier"],
                    source=r["source_desc"], url=r["source_url"], note=r["note"])
        v = float(r["value"])
        if r["obs_type"] not in DIRECT:
            if r["metric"] == "WS":
                usd_bbl = v * FLAT_2026 / 100 / BPT_TD3C
                derived.append(dict(base, metric="WS", value=v, usd_bbl=round(usd_bbl, 2), method="WS×平准费率÷桶/吨"))
            else:
                month = r["obs_date"][5:7]
                ws, bbl = model_bbl(v, month, p)
                derived.append(dict(base, metric="TCE_usd_day", value=v, usd_bbl=round(bbl, 2),
                                    method=f"模型估计：TCE→WS {ws:.1f}，燃油 {BUNKER[month]}"))
            continue
        if r["metric"] == "WS":
            usd_t = v * FLAT_2026 / 100
            direct.append(dict(base, ws=v, usd_t=round(usd_t, 2), usd_bbl=round(usd_t / BPT_TD3C, 2)))
        else:
            month = r["obs_date"][5:7]
            ws, bbl = model_bbl(v, month, p)
            _, lo = model_bbl(v, month, p, BUNKER[month] - 100, 100000)
            _, hi = model_bbl(v, month, p, BUNKER[month] + 100, 250000)
            tce.append(dict(base, tce_usd_day=int(v), model_ws=round(ws, 1), model_usd_bbl=round(bbl, 2),
                            model_low=round(lo, 2), model_high=round(hi, 2), bunker_assumed=BUNKER[month]))

    ct1 = [dict(obs_date=r["obs_date"], usd_t=float(r["value"]), usd_bbl=round(float(r["value"]) / BPT_CT1, 2),
                obs_type=r["obs_type"], tier=r["tier"], source=r["source_desc"], url=r["source_url"], note=r["note"])
           for r in raw if r["series"] == "CT1"]
    td22 = [dict(obs_date=r["obs_date"], lumpsum_musd=float(r["value"]),
                 usd_bbl=round(float(r["value"]) * 1e6 / TD22_BBL, 2),
                 obs_type=r["obs_type"], tier=r["tier"], source=r["source_desc"], url=r["source_url"], note=r["note"])
            for r in raw if r["series"] == "TD22"]
    budget = []
    for r in raw:
        if r["series"] in ("PERIOD_TC", "FFA_Q4"):
            month = r["obs_date"][5:7]
            ws, bbl = model_bbl(float(r["value"]), month, p)
            budget.append(dict(obs_date=r["obs_date"],
                               kind="1 年期租" if r["series"] == "PERIOD_TC" else "Q4-26 TD3C FFA",
                               tce_usd_day=int(float(r["value"])), budget_usd_bbl=round(bbl, 2),
                               bunker_assumed=BUNKER[month], obs_type=r["obs_type"], tier=r["tier"],
                               source=r["source_desc"], url=r["source_url"], note=r["note"]))

    by = {d["obs_date"]: d for d in direct}
    share = []
    for pr in prices:
        if pr["item"] != "BRENT" or pr["obs_date"] not in by:
            continue
        f = by[pr["obs_date"]]["usd_bbl"]
        oil = float(pr["value"])
        share.append(dict(obs_date=pr["obs_date"], freight_usd_bbl=f, brent=oil, brent_basis=pr["basis"],
                          freight_over_oil=round(f / oil, 4), freight_over_delivered=round(f / (oil + f), 4),
                          freight_source="TD3C 直接报价（WS）", brent_source=pr["source_desc"], brent_url=pr["source_url"]))

    obs = [dict(obs_id=f"{r['series']}-{r['obs_date']}-{r['metric']}", **r) for r in raw]
    for name, rows in [("observations", obs), ("td3c_direct", direct), ("td3c_tce", tce), ("td3c_derived", derived), ("ct1", ct1),
                       ("td22", td22), ("period_budget", budget), ("share", share)]:
        write(os.path.join(a.dir, name + ".csv"), rows)
        print(f"{name}.csv: {len(rows)} 行")


if __name__ == "__main__":
    main()
