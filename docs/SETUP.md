# 配置与部署准备

项目默认仍以只读方式运行，但预约提交链路已经实现。只有明确传入
`--allow-booking` 时才会提交，配置示例中的定时任务和监控任务也默认关闭。

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
3. 输出当前未结束预约的数量。

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
```

请不要在聊天中发送密码或 TOTP 种子。

建立并添加 GitHub Secrets 后，可以在 Actions 页面手动运行只读探测、定时策略演练和监控。
预约工作流的 `allow_booking` 输入默认是关闭的；第一次真实预约前，应先运行不带该开关的
演练，确认日期、场馆、时段和策略输出正确，再手动打开一次。

## 公开仓库是否可行

可以。公开仓库的代码、配置示例和工作流会对所有人可见，但 GitHub Secrets 的值不会因为仓库
公开而显示。GitHub 官方目前对公开仓库的标准 GitHub-hosted runner 不收取 Actions 分钟费用，
因此公开仓库更适合承载五分钟一次的监控任务。

公开后必须遵守以下边界：

1. 不提交 `.env`、密码、TOTP 种子、SMTP 授权码、Cookie、CAS ticket 或个人手机号。
2. 预约和监控工作流只允许在默认分支的 `schedule` 或手动触发运行，不在外部 Pull Request 中使用 Secrets。
3. 固定第三方 Action 版本，并保留 `contents: read` 等最小权限。
4. 公开仓库中的 Issue、日志和失败输出不得包含账号、邮箱、姓名或预约接口原始响应。

公开仓库解决的是 Actions 免费额度问题，不会解决 GitHub 定时任务可能延迟、站点限流或瑞数
校验变化的问题；正式启用前仍需先完成不提交演练。

## 后续需要添加的 GitHub Secrets

```text
FUDAN_USERNAME
FUDAN_PASSWORD
FUDAN_TOTP_SECRET       可选
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

每次提交前都会重新检查未结束预约数量。提交期间若空位被抢走，会输出
`slot_unavailable` 并继续尝试其他候选，不会使任务失败；达到三个未结束预约后只监控、不再提交。

如果提交接口返回 HTTP 412，则表示预约接口另外要求瑞数或预约验证码令牌。程序会停止并明确报告
这个问题，不会把它误判成“场地已被抢走”。只读查询通过并不代表预约提交一定已经通过同一层校验。

## Cloudflare

Cloudflare Worker 暂不部署预约系统监控。Worker 运行时不能直接运行当前所需的 Node/JSDOM
校验桥，而且跨平台保存预约 Cookie 会扩大泄露风险。等只读桥和监控策略稳定后，再单独评估
是否使用其他支持 Node 长连接的免费平台；不会把当前会话材料写入 Cloudflare KV。
