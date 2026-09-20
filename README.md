# Fudan Booking Bot

复旦体育场馆定时预约与空位监控工具。

默认以只读联调为主：配置校验、最多三个未结束预约的容量控制、连续时段优先策略、QQ SMTP 通知适配器、UIS 登录和网页日历查询已经建立。受控的体育场馆预约提交也已加入，但必须显式传入 `--allow-booking`，配置示例中的预约任务默认关闭。

预约站点目前有瑞数 JavaScript 校验。项目已加入不启动浏览器的 Node/JSDOM 校验桥；本地和
GitHub Actions 需要 Node 24 与 challenge 依赖，Python 会在同一进程会话中查询只读接口。

## 已确认规则

- 每天 07:00 开放后天场地。
- 最多允许三个未结束预约。
- 子场地由系统随机分配，配置单位是“场馆 + 球类”。
- 达到三个预约后继续监控和提醒，但不再自动预约。
- 本项目永远不会自动取消已有预约。
- GitHub Actions 计划为 06:50 启动、06:55 登录、07:00 提交。
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

真实凭据只能放在 GitHub Secrets、Cloudflare Secrets 或未提交的本地 `.env` 中。预约时默认使用系统账号侧已保存的联系方式；不要把密码、SMTP 授权码、Cookie 或 TOTP 种子写进配置文件。

更多信息见：

- [架构说明](docs/ARCHITECTURE.md)
- [安全说明](docs/SECURITY.md)
- [部署准备](docs/SETUP.md)
- [实施路线与用户介入点](docs/ROADMAP.md)
