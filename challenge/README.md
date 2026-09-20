# CAS 校验桥

该目录是一个只在本机进程中运行的 Node 辅助程序。它接收 Python 已经从 UIS 获取的
预约系统 CAS 票据 URL，执行预约站点下发的瑞数 JavaScript，并在同一个 Node 进程中保持预约系统会话。

- 不启动 Chromium、Playwright 或其他浏览器；
- 只接受 `booking.fudan.edu.cn` 的 HTTPS 票据 URL；
- 票据只通过标准输入传递，不写入日志、文件或 GitHub Actions 输出；
- 标准输入输出使用逐行 JSON 协议，便于 Python 读取接口响应；
- 默认只允许场馆列表、日程和预约列表三个 GET 接口；另有一个固定的
  `book_resource` 操作，只能 POST 体育场馆预约端点，不接受任意 URL、取消端点或其他 POST；
- `sdenv` 版本固定，避免反爬脚本变化时无提示升级。
