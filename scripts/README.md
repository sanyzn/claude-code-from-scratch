# tq_option_daily_chart.py

从天勤 (TqSdk) 下载合约日线数据并绘制 K线 / 成交量 / 持仓量 三联图。

默认目标合约 `sc2505-p-510` → 天勤代码 `INE.sc2505P510`
(上期能源 原油期权，sc2505 系列，认沽，行权价 510)。

## 依赖

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install tqsdk pandas matplotlib
```

## 前置条件：天勤账户

TqSdk 3.x 起，拉取任何行情都必须登录快期/天勤账户
(免费注册: https://account.shinnytech.com/)。

```bash
export TQ_USER=你的账号
export TQ_PASSWORD=你的密码
```

## 用法

```bash
# 下载 sc2505-p-510 最近 400 根日线并出图
python scripts/tq_option_daily_chart.py

# 换合约 / 换天数
python scripts/tq_option_daily_chart.py --symbol sc2505-c-510 --days 500

# 离线：用已下好的 CSV 直接重画
python scripts/tq_option_daily_chart.py --from-csv out/sc2505P510_daily.csv
```

输出到 `out/`：

- `<合约>_daily.csv` — datetime, open, high, low, close, volume, open_oi, close_oi
- `<合约>_daily.png` — 上：K线(红涨绿跌)；中：成交量；下：持仓量

## 网络要求

脚本需要访问以下天勤域名，缺一不可：

- `auth.shinnytech.com` — 账户鉴权
- `openmd.shinnytech.com` — 行情/历史K线 (WebSocket)

在受限网络环境（如带 egress 白名单的沙箱）中，这两个域名会在 CONNECT
阶段被拒 (403)，脚本会在鉴权步骤直接失败。此时请在本地或允许出网的机器上运行。
