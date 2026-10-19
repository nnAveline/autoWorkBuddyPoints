# WorkBuddy 每日签到 + 派猫猫旅行

每天定时执行：

1. 查询「Buddy 加油站」签到状态，未签到则领取；
2. 查询猫猫旅行状态，先领回到达积分，再判断是否能派新的一趟；
3. 把签到和猫猫结果分开写进同一张飞书卡片。

猫猫部分失败不会影响签到结论；签到成功即视为本次任务成功。

## 已确认接口

桌面端和 Web 端都走 `https://www.workbuddy.cn`，以下路径均已用真实账号验证：

| 用途 | 方法 | 路径 |
|---|---|---|
| 签到状态 | POST | `/v2/billing/meter/checkin-activity-status` |
| 执行签到 | POST | `/v2/billing/meter/daily-checkin` |
| 猫猫状态 | GET | `/v2/activity/growth/buddy/travel/status` |
| 领取到达积分 | POST | `/v2/activity/growth/buddy/travel/claim` |
| 派猫猫出发 | POST | `/v2/activity/growth/buddy/travel/depart` |

`depart` 请求体为 `{"location_id": 1}`（咖啡馆，约 3 小时到达）。可用仓库变量 `WB_TRAVEL_LOCATION_ID` 覆盖。

## 一次性配置

### 1. 创建飞书机器人

飞书群 → 设置 → 群机器人 → 添加「自定义机器人」，复制 Webhook 地址。

### 2. 导入本机登录态

WorkBuddy 的登录态由 Electron 静态钥加密，必须用 WorkBuddy 自带的 Electron 运行时读取：

```bash
python3 scripts/refresh_token.py --repo <owner>/<repo>
```

脚本会：

- 用 `command -v` 动态定位 `python3` / `gh`；
- 自动查找 WorkBuddy 二进制和 `app.asar`（也可用 `WB_BIN` / `WB_ASAR` 指定）；
- 解密 `CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`；
- 有 `gh` 时执行 `gh secret set` 写入 `WB_ACCESS_TOKEN`、`WB_USER_ID`、`WB_DOMAIN`、`WB_ENTERPRISE_ID`；
- 没有 `gh` 时写到 `~/.workbuddy/cache/workbuddy-daily/secrets.env`（0600），按提示在 GitHub 网页手动创建 secret。

token 不会打印到终端，也不会写进仓库。

### 3. 添加 GitHub secrets

仓库 Settings → Secrets and variables → Actions：

| Secret | 必填 | 说明 |
|---|---|---|
| `WB_ACCESS_TOKEN` | 是 | 本机导出的 accessToken |
| `WB_USER_ID` | 否 | WorkBuddy 账号 uid；不填则从 token 的 `sub` 解析 |
| `FEISHU_WEBHOOK` | 是 | 飞书自定义机器人 Webhook |
| `FEISHU_SECRET` | 否 | 机器人开启「签名校验」时才需要 |
| `WB_DOMAIN` | 否 | 默认 `www.workbuddy.cn` |
| `WB_ENTERPRISE_ID` | 否 | 企业账号才需要 |

可选仓库变量 `WB_API_BASE`，默认 `https://www.workbuddy.cn`。

### 4. 启用 Actions

把仓库推到 GitHub 默认分支后，在 Actions 页面启用 workflow。`schedule` 只在默认分支生效；定时为每天 09:17（Asia/Shanghai），也可手动 `Run workflow`。

## 本地试跑

```bash
set -a
. ~/.workbuddy/cache/workbuddy-daily/secrets.env
set +a

FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/...' \
python3 scripts/wb_daily.py
```

只看 API 返回、不推送飞书：

```bash
python3 scripts/wb_daily.py --dry-run
```

## 行为说明

- 签到：`today_checked_in=true` 时只报告，不重复领取；活动未开启则明确报错。
- 猫猫：`state=arrived`（或 `traveling` 且已过 `arrive_at`）时先调 `claim`；`claim` 返回「no unclaimed travel」视为已领。
- 领取后重新查状态：`traveling` 表示今天已派过；`daily_limit_reached=true` 表示今日次数用完；只有 `idle` 且未达上限才调 `depart`。
- 退出码：签到成功为 0；签到失败为 1。猫猫失败不改变退出码。
- 飞书推送失败会在日志和 `result.md` 中标记，但签到成功时仍返回 0。

## 安全

- 仓库里没有任何 token；`secrets.env`、`result.*` 已加入 `.gitignore`。
- workflow 只通过环境变量读取 secrets，不拼进命令行，不打印 Authorization。
- accessToken 有效期约 28 天，过期后重新执行 `refresh_token.py` 更新 secret。
