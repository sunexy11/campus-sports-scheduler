# 配置与部署准备

项目默认仍以只读方式运行，但预约提交链路已经实现。只有明确传入
`--allow-booking` 时才会提交；配置示例保留了实际的定时预约和监控目标，但 GitHub 手动触发与
Cron-job.org 请求都可以通过 `allow_booking: false` 先做只读演练。

## 本地安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

先校验示例配置：

```bash
fudan-booking validate-config --config config/config.example.yaml
```

## 安全执行只读联调

推荐使用交互式输入，避免把密码写入命令、Shell 历史或文件：

```bash
fudan-booking probe --prompt-credentials
```

命令会要求输入学号和密码，其中密码不会回显。它只会：

1. 登录复旦统一身份认证和预约系统；
2. 列出“运动场馆预约”专题中的场馆编号与名称；
3. 输出当前未结束预约的数量，以及按总上限 3 计算出的剩余预约额度。

注意：如果在最后一步看到“`UIS CAS 票据兑换 HTTP 412`”，说明预约站点下发了瑞数
JavaScript 反爬校验。此时账号密码通常已经通过 UIS，失败点在预约站点的前置校验，
不是 `.env` 或密码格式错误；在校验桥接方案确定前，不要反复尝试或把 Cookie 发到聊天中。

若要读取某个场馆在某一天的日程，可使用上一步返回的场馆编号：

```bash
fudan-booking probe --prompt-credentials --resource-id 938 --date 2026-09-22
```

`--resource-id` 和 `--date` 必须同时提供。输出包含各时段是否有空位和空余子场地数量，不包含已预约人的信息。该命令不会提交或取消预约。

如果希望本地保存凭据，请复制空白模板：

```bash
cp .env.example .env
```

然后只编辑 `.env`，不要编辑 `.env.example`。程序启动时会自动读取项目当前目录下的 `.env`，但不会覆盖系统已经注入的环境变量。`.env` 已加入 Git 忽略列表，不应上传 GitHub；`.env.example` 会上传，因此必须一直保持空白。

配置完成后可以省略交互输入参数：

```bash
fudan-booking probe
```

自动化环境中也可以通过环境变量提供凭据：

```text
FUDAN_USERNAME
FUDAN_PASSWORD
FUDAN_TOTP_SECRET       可选，仅在 UIS 明确要求 TOTP 时使用
FUDAN_MOBILE            可选；推荐填写，避免预约开始后再读取个人资料
```

请不要在聊天中发送密码、手机号或 TOTP 种子。`FUDAN_MOBILE` 如果不设置，程序会继续从已登录的
个人资料中读取手机号作为备用方案。

建立并添加 GitHub Secrets 后，可以在 Actions 页面手动运行只读探测、定时策略演练和监控。
预约工作流的 `allow_booking` 输入默认是关闭的；第一次真实预约前，应先运行不带该开关的
演练，确认日期、场馆、时段和策略输出正确，再手动打开一次。

## 公开仓库是否可行

可以。公开仓库的代码、配置示例和工作流会对所有人可见，但 GitHub Secrets 的值不会因为仓库
公开而显示。GitHub 官方目前对公开仓库的标准 GitHub-hosted runner 不收取 Actions 分钟费用，
因此公开仓库更适合承载五分钟一次的监控任务。

公开后必须遵守以下边界：

1. 不提交 `.env`、密码、TOTP 种子、SMTP 授权码、Cookie、CAS ticket 或个人手机号。
2. 预约和监控工作流只允许在默认分支的 `workflow_dispatch` 触发运行，不在外部 Pull Request 中使用 Secrets。
3. 固定第三方 Action 版本，并保留 `contents: read` 等最小权限。
4. 公开仓库中的 Issue、日志和失败输出不得包含账号、邮箱、姓名或预约接口原始响应。

公开仓库解决的是 Actions 免费额度问题；本项目进一步使用 Cron-job.org 触发 `workflow_dispatch`
来降低 GitHub 原生定时任务的延迟风险，但仍不能消除 Runner 排队、站点限流或瑞数校验变化。
正式启用前仍需先完成不提交演练。

## 三个地方分别负责什么

请把配置分成三层理解，不要混在一起：

1. `config/config.example.yaml` 是“预约什么”。这里修改场馆、球类、日期、时间段、优先级和
   `max_new_reservations`，并随代码一起提交到仓库。
2. `.github/workflows/*.yml` 是“如何运行”。`allow_booking` 默认值只影响你在 GitHub 页面上
   手动点击 **Run workflow** 时的表单默认值；`wait_until` 默认值只影响没有通过请求体传入时间时的
   手动运行。正常使用 Cron-job.org 时，不要为了每次测试去改这些默认值。
3. Cron-job.org 请求体是“这一次是否真的提交”。定时预约请求传
   `allow_booking: true` 和 `wait_until: "07:00"`；监控请求传 `allow_booking: true` 或 `false`。
   它只决定本次运行是否允许提交，不改变仓库配置。

`monitor.jobs[].mode` 和 `allow_booking` 看起来相似，但职责不同：

- `mode: monitor_only`：这个目标永远只报告空位，不会预约；即使本次 `allow_booking: true` 也不会提交。
- `mode: auto_book_if_capacity`：这个目标具备自动预约资格，但只有本次运行的 `allow_booking: true` 时才会提交。
- `allow_booking` 是一个总开关，专门防止 Cron 请求或手动测试意外产生真实预约。

因此，通常只需要改 `config/config.example.yaml` 的目标；上线前把 Cron-job.org 请求体中的
`allow_booking` 从 `false` 改为 `true`。工作流默认值保持不动即可。

## Cron-job.org 自动触发

定时预约和五分钟监控的外部触发配置见 [Cron-job.org 配置指南](CRON_JOB_ORG.md)。该方案需要一个
只授予目标仓库 `Actions: Read and write` 权限的 GitHub fine-grained Token。Token 只放在
Cron-job.org 请求 Header 中，不写入仓库和 GitHub Secrets。

## 后续需要添加的 GitHub Secrets

```text
FUDAN_USERNAME
FUDAN_PASSWORD
FUDAN_TOTP_SECRET       可选
FUDAN_MOBILE            可选；预约表单使用的手机号
QQ_SMTP_USERNAME
QQ_SMTP_AUTH_CODE
NOTIFICATION_EMAIL
```

## QQ 邮箱

在 QQ 邮箱设置中开启 IMAP/SMTP 服务，并生成单独的授权码。把授权码保存为 `QQ_SMTP_AUTH_CODE`，不要使用或暴露 QQ 密码。

## Node/JSDOM 校验桥

预约站点目前会在 CAS 兑换和业务接口前返回瑞数 JavaScript 校验。项目使用
`challenge/cas_bridge.mjs` 在 GitHub Actions 内执行该校验，不启动浏览器。桥接进程只允许
读取场馆、日程和预约列表，并额外允许固定的体育场馆预约端点；默认使用账号侧已填写的联系方式。

本地需要 Node 24 和 pnpm。确认 `node --version` 为 24.x 后，可以把 `FUDAN_NODE_BIN=node`
写入未提交的 `.env`：

```bash
cd challenge
pnpm install --frozen-lockfile
```

然后运行探测或演练时设置 `FUDAN_NODE_BIN=node`。GitHub Actions 会自动安装 Node 24、pnpm
和桥接依赖。CAS 票据只通过标准输入传给桥接进程，不写入日志。

## 本地预约演练和真实预约

先运行定时策略演练（只查询，不提交）：

```bash
fudan-booking scheduled-book-once --config config/config.example.yaml
```

监控任务也默认只提醒；只有监控任务设置为 `auto_book_if_capacity`，并显式增加开关时才会预约：

```bash
fudan-booking monitor-once --config config/config.example.yaml --allow-booking
```

每次运行前会从网站重新读取未结束预约数量，并按账户剩余额度限制本次提交。提交期间若空位被抢走，会输出
`slot_unavailable` 并继续尝试其他候选，不会使任务失败；达到三个未结束预约后跳过本次查询和预约。
其中 `monitor.jobs` 可以配置多个任务。每个任务的 `dates` 可写成偏移量列表，例如
`[0, 1, 2]` 表示今天、明天、后天，`[1, 2]` 表示明天和后天，`[]` 表示本轮跳过日期查询；
原来的 `next_3_days` 和明确的 `YYYY-MM-DD` 日期列表仍兼容。多个任务共享总计三个未结束预约的
上限；`max_new_reservations` 只是单个任务在本次运行中的新增预约上限。

如果你已经预约过某个时间段，提交接口可能返回“预约时间不可重叠”。程序会把它记录为
`overlap_with_existing`，跳过当前候选并继续尝试其他候选，不会因此终止本轮监控或定时抢场。

如果只读业务接口或预约提交返回 HTTP 412，Node/JSDOM 桥会执行同域瑞数挑战 HTML；现在既监听页面的完成事件，
也检测 Cookie 是否已经更新，任一信号出现就会提前重试，不再盲等完整超时时间。第一次挑战默认最多
等待 8 秒并重试一次；如果仍返回 412，会再执行第二轮挑战，默认最多等待 8 秒并进行最后一次提交。
等待时间可以分别通过 `FUDAN_POST_CHALLENGE_TIMEOUT_MS`（首轮）和
`FUDAN_POST_RETRY_CHALLENGE_TIMEOUT_MS`（第二轮）覆盖；环境变量名为兼容早期仅处理预约提交的版本而保留。
默认值均为 `8000` 毫秒；如果 Cookie
提前更新，实际等待时间会短于 8 秒。

定时预约在等待 `wait_until` 之前会先读取场馆列表、预热目标日期日历并准备手机号；等待结束后仍会
刷新一次日历，因为开放时刻的空位状态可能与预热结果不同。预约结果中的
`challenge_completion_signal` 和 `retry_challenge_completion_signal` 会标记两轮挑战分别是由页面事件、
Cookie 更新还是超时结束。只读 GET 的失败日志还会显示各次 GET 的耗时。若最终仍为 HTTP 412，
错误信息会带上两轮挑战的耗时和完成信号，便于查看
GitHub Actions 上 Cookie 实际更新用了多久。

预约或监控开始前，程序会从网站读取当前未结束预约数，并用“3 减去已有数量”和任务的
`max_new_reservations` 取较小值作为本次最多新增数量。已有预约达到 3 个，或某个任务的
`max_new_reservations` 为 0 时，该任务会直接跳过，不再查询场地。定时预约、监控自动预约和
定时预约成功或失败时会发送简短结果邮件；监控模式只有发现至少一个空位时才发送邮件，邮件列出
有空位的场馆、日期、时间和空余数量。自动预约监控还会标注每个空位的预约结果。

## Cloudflare

Cloudflare Worker 暂不部署预约系统监控。Worker 运行时不能直接运行当前所需的 Node/JSDOM
校验桥，而且跨平台保存预约 Cookie 会扩大泄露风险。等只读桥和监控策略稳定后，再单独评估
是否使用其他支持 Node 长连接的免费平台；不会把当前会话材料写入 Cloudflare KV。
