# Campus Sports Scheduler

校园运动场地定时预约与空位监控工具。当前适配复旦体育场馆预约站点，项目名称保持中性，
方便公开仓库使用。

默认以只读联调为主：配置校验、最多三个未结束预约的容量控制、连续时段优先策略、QQ SMTP 通知适配器、UIS 登录和网页日历查询已经建立。受控的体育场馆预约提交也已加入，但必须显式传入 `--allow-booking`。

预约站点目前有瑞数 JavaScript 校验。项目已加入不启动浏览器的 Node/JSDOM 校验桥；本地和
GitHub Actions 需要 Node 24 与 challenge 依赖。预约列表、场馆列表、日历等只读请求和预约提交
遇到 HTTP 412 时，都会在同一会话内更新 Cookie 并有限重试。

## 已确认规则

- 每天 07:00 开放后天场地。
- 最多允许三个未结束预约。
- 子场地由系统随机分配，配置单位是“场馆 + 球类”。
- 网站已有三个未结束预约时，本轮直接跳过查询和预约；不会取消已有预约。
- 本项目永远不会自动取消已有预约。
- 定时预约和五分钟监控统一通过 Cron-job.org 调用 GitHub `workflow_dispatch`，不依赖 GitHub 原生 `schedule` 的准点性。
- 定时预约到点后在同一登录会话内持续重试最多 180 秒；达到本次上限、账号容量耗尽或时间窗结束后退出。
- 监控暂不部署到 Cloudflare Worker；当前 Node/JSDOM 会话桥先在 GitHub Actions/Node 环境验证。
- 通知仅使用 QQ SMTP。

## 本地检查

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cd challenge && pnpm install --frozen-lockfile && cd ..
fudan-booking validate-config --config config/config.example.yaml
pytest
```

安全执行只读登录与查询（密码不会回显或保存）：

```bash
fudan-booking probe --prompt-credentials
```

也可以把 `.env.example` 复制为不会提交的 `.env`，在 `.env` 中填写本地凭据后运行 `fudan-booking probe`。切勿把真实值写入 `.env.example`。

预约策略可以先做不提交演练：

```bash
fudan-booking scheduled-book-once --config config/config.example.yaml
```

确认输出无误后，才在手动 Actions 或本地命令中增加 `--allow-booking`。监控任务同理；
空位在提交前被别人抢走时会记录为 `slot_unavailable`，继续尝试其他候选，不会中断后续监控。

真实凭据只能放在 GitHub Secrets、Cloudflare Secrets 或未提交的本地 `.env` 中。预约手机号可以通过可选的
`FUDAN_MOBILE` Secret 提供；不设置时会从账号资料读取。不要把密码、手机号、SMTP 授权码、Cookie 或 TOTP 种子写进配置文件。

## 公开仓库安全边界

公开仓库不会隐藏代码，只能降低偶然被注意到的概率。不要把账号、密码、手机号、Cookie、CAS
票据、QQ 授权码或 GitHub Token 写入代码、配置、Issue、日志或 README。真实值只放在 GitHub
Secrets 或 Cron-job.org 的请求 Header 中；`.env.example` 永远只保留空白模板。

## 自动触发

项目不再使用 GitHub 原生 `schedule`。请按照 [Cron-job.org 配置指南](docs/CRON_JOB_ORG.md)
创建两个外部定时任务：每天 06:50 触发定时预约，以及 07:00–23:55 每 5 分钟触发一次监控。

更多信息见：

- [架构说明](docs/ARCHITECTURE.md)
- [安全说明](docs/SECURITY.md)
- [部署准备](docs/SETUP.md)
- [实施路线与用户介入点](docs/ROADMAP.md)
