# Cron-job.org 自动触发指南

项目使用 Cron-job.org 作为“准点闹钟”，使用 GitHub API 的 `workflow_dispatch` 启动 Actions。
Cron-job.org 不会接触复旦账号密码；它只向 GitHub 发送启动请求。

## 你需要准备的两个东西

### 1. GitHub fine-grained Token

在 GitHub 中进入：

`Settings → Developer settings → Personal access tokens → Fine-grained tokens`

创建 Token 时这样选择：

1. Repository access 选择 `Only select repositories`；
2. 只选择 `campus-sports-scheduler`；
3. Repository permissions 只打开 `Actions: Read and write`；
4. 设置一个有效期，例如 90 天；
5. 创建后立即复制 Token，只需要把它粘贴到 Cron-job.org，不要提交到项目。

这个 Token 可以触发工作流，但不能读取整个仓库，也不能读取 GitHub Secrets。

### 2. Cron-job.org 账号

打开 [cron-job.org](https://cron-job.org/)，注册并登录。每一个定时任务都需要填写：

- URL；
- 请求方法 `POST`；
- 请求 Header；
- JSON 请求体；
- 时区和执行时间。

下面两个任务都使用同一组 Header：

```text
Authorization: Bearer 你的_FINE_GRAINED_TOKEN
Accept: application/vnd.github+json
Content-Type: application/json
X-GitHub-Api-Version: 2022-11-28
```

不要把 Token 放在 URL、请求体、任务名称或备注中。

## 任务一：每天 06:50 触发定时预约

创建 Cron Job，设置：

- Title：`campus booking at open`
- Timezone：`Asia/Shanghai`
- 执行时间：每天 `06:50`
- Request method：`POST`
- URL：

```text
https://api.github.com/repos/sunexy11/campus-sports-scheduler/actions/workflows/book-at-open.yml/dispatches
```

- Request body：

```json
{
  "ref": "main",
  "inputs": {
    "allow_booking": "true",
    "wait_until": "07:00",
    "retry_window_seconds": "180"
  }
}
```

运行逻辑是：06:50 左右触发 Actions，环境准备和登录完成后等到 07:00，然后在同一个登录会话中
持续查询和预约最多 180 秒。达到本次新增上限、账号没有剩余额度或 180 秒到期时结束；不会主动重新登录。

第一次测试时，把 `allow_booking` 改成 `false`，只检查日志中的计划和时间；确认无误后再改回
`true`。Cron-job.org 的 Test run 也只能先使用 `false`，不要直接用真实预约测试。

## 任务二：07:00–23:55 每 5 分钟触发监控

再创建一个 Cron Job，设置：

- Title：`campus booking monitor`
- Timezone：`Asia/Shanghai`
- 执行频率：每 5 分钟
- 执行时间范围：每天 07:00 到 23:55
- Request method：`POST`
- URL：

```text
https://api.github.com/repos/sunexy11/campus-sports-scheduler/actions/workflows/monitor-once.yml/dispatches
```

- Request body：

```json
{
  "ref": "main",
  "inputs": {
    "allow_booking": "true"
  }
}
```

如果只想收邮件、不允许自动预约，把 `allow_booking` 改成 `false`。

每次 Actions 只查询一次，结束后退出；下一次由 Cron-job.org 在 5 分钟后再次触发。达到三个
未结束预约后，程序会直接跳过本次监控和预约，不会继续请求场地，也不会取消已有预约。

## 第一次上线顺序

1. 先推送代码并确认两个 Workflow 在 GitHub Actions 页面可见；
2. 在 GitHub Secrets 中确认 `FUDAN_USERNAME`、`FUDAN_PASSWORD`、QQ SMTP 等变量仍存在；
3. 在 Cron-job.org 创建两个任务，但暂时都用 `allow_booking: false`；
4. 用 Cron-job.org 的 Test run 测试 GitHub API 请求，HTTP 204 表示触发成功；
5. 在 GitHub Actions 页面确认定时预约和监控都真的启动；
6. 确认日志中的日期、场馆和时间正确后，只把需要自动预约的任务改成 `true`；
7. 保留 Cron-job.org 的执行历史，出现异常时先看它是否成功收到 GitHub 的 204 响应，再看 Actions 日志。

## 常见问题

### Cron-job.org 显示成功，但 Actions 没有运行

检查 URL 中的仓库名和 Workflow 文件名是否准确，Token 是否仍有效，以及 Token 是否拥有目标仓库
的 `Actions: Read and write` 权限。

### Actions 还是晚了几分钟

Cron-job.org 负责准时发起 API 请求，但 GitHub 仍需分配 Runner，不能承诺绝对零延迟。定时预约
工作流内部会等待到 `07:00`；如果 Runner 在 07:00 之后才启动，程序只能立即执行，无法追回已经错过的
开放时刻。

### 为什么不把 Token 放到 GitHub Secrets

因为触发请求来自 Cron-job.org，GitHub Secrets 不会自动提供给外部服务。这个 Token 只放在
Cron-job.org 的 Header 中，权限要限制到单个仓库的 Actions 写权限，并设置有效期。
