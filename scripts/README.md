# tq_option_daily_chart.py

下载合约日线数据并绘制 K线 / 成交量 / 持仓量 三联图。

默认目标合约 `sc2505-p-510` → `sc2505P510`
(上期能源 原油期权，sc2505 系列，认沽，行权价 510)。

## 依赖

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pandas matplotlib requests akshare   # akshare 仅 sina 源与交易日历需要
pip install tqsdk                                # 仅 tqsdk 源需要
```

## 三个数据源

| `--source` | 免费 | 需账户 | 字段 | 请求数 |
|---|---|---|---|---|
| `ine` (默认优先) | ✅ | 否 | OHLC + 结算价 + 成交量 + **持仓量** + Delta | 每个交易日 1 次，带本地缓存 |
| `sina` | ✅ | 否 | OHLC + 成交量（**无持仓量**） | 1 次 |
| `tqsdk` | 需订阅 | 是 | OHLC + 成交量 + 持仓量 | 1 次 |

- `ine`：抓交易所自己发布的期权日行情文件
  `https://www.ine.cn/data/tradedata/option/dailydata/kx<YYYYMMDD>.dat`
  （取不到时回落到 `www.shfe.com.cn` 同名路径），逐日筛出目标合约。
  交易日历取自 akshare 本地日历文件，不联网。原始 .dat 缓存在 `.cache/ine_daily/`，
  重跑不会重复下载。期权无成交日 OHLC 为空，脚本用当日结算价补成平线，保持序列连续。
- `sina`：`ak.option_commodity_hist_sina(symbol="sc2505P510")`。一次请求就够，
  但新浪商品期权页面的品种列表未必包含原油期权，取不到属正常，会自动回落。
- `tqsdk`：TqSdk 3.x 起拉行情必须登录快期/天勤账户
  （免费注册 https://account.shinnytech.com/）：

  ```bash
  export TQ_USER=你的账号 TQ_PASSWORD=你的密码
  ```

`--source auto`（默认）按 `ine → sina → tqsdk` 依次尝试，全失败时打印每个源的具体原因。

## 用法

```bash
python scripts/tq_option_daily_chart.py                       # auto
python scripts/tq_option_daily_chart.py --source ine
python scripts/tq_option_daily_chart.py --source ine --start 2024-05-01 --end 2025-05-31
python scripts/tq_option_daily_chart.py --symbol sc2505-c-510 --source sina
python scripts/tq_option_daily_chart.py --from-csv out/sc2505P510_daily.csv   # 离线重画
```

不给 `--start/--end` 时，按合约月份自动推算区间（到期月末往前 560 天，右端不超过今天）。

输出到 `out/`：

- `<合约>_daily.csv`
- `<合约>_daily.png` — 上：K线(红涨绿跌)；中：成交量；下：持仓量（`sina` 源没有持仓量，
  会自动省掉第三格）

## 网络要求

按数据源，需要访问：

- `ine` → `www.ine.cn`、`www.shfe.com.cn`
- `sina` → `stock.finance.sina.com.cn`
- `tqsdk` → `auth.shinnytech.com`、`openmd.shinnytech.com`

在带出网白名单的沙箱里，以上域名会在 CONNECT 阶段被拒 (403)，任何数据源都取不到数据。
请在本地或允许出网的机器上运行。
