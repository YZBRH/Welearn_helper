# 声明

本项目原作者为https://github.com/Avenshy

原项目已停止更新维护,无法直接使用,现进行修正调整后已恢复其原功能

该脚本版权归于原作者,本项目仅进行修复与优化

目前维护者：

- [YZBRH](https://github.com/YZBRH/)
- [lxbsdsg](https://github.com/lxbsdsg/)

# 功能

- 刷完成度正确率：每个练习可指定固定正确率（如 100）或随机区间（如 70,100）
- 刷学习时长：每个练习可指定固定时长秒（如 30）或随机区间（如 10,30）
- Web 图形界面：任务在后台线程运行不卡顿、可随时停止、日志实时滚动
- 支持多选单元、单元内多选小节（不勾选小节 = 刷整个单元）
- 切换刷课模式时复用登录会话，无需重新登录
- 保留命令行入口

# 使用

## 环境准备

本机运行需已安装 Python 3 并加入 PATH，然后安装依赖：

```
pip install -r requirement.txt
```

## 图形界面(推荐)

双击 一键启动.bat，或手动运行：

```
python -u web_gui.py
```

启动后自动打开浏览器 http://127.0.0.1:17777/ ，按 Ctrl+C 退出。

操作流程：输入账号密码并选择模式 → 登录 → 选择课程 → 勾选单元（或勾选“全部单元”）→ 可展开单元勾选小节 → 填写参数 → 开始任务。任务运行中可随时点击“停止”。

可选环境变量：

- `WELEARN_PORT`：修改监听端口（默认 17777）
- `WELEARN_NO_OPEN=1`：跳过自动打开浏览器（适合无桌面/远程环境）

## 打包 / 使用 Windows 可执行文件

从 GitHub Release 下载 `WelearnHelper.exe` 后直接双击运行，无需安装 Python；程序会在本机启动 Web 图形界面并自动打开浏览器。

本仓库维护者可重新打包：

```
python -m pip install -r requirement.txt -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1
```

打包产物为 `dist/WelearnHelper.exe`。推送 `v*` 标签后，GitHub Actions 会运行测试、打包并把 EXE 与 SHA-256 校验文件上传到 GitHub Release。

## 命令行
双击 命令行启动.bat，或手动运行：

```
python -u main.py
```

按菜单提示操作：1 刷完成度正确率，2 刷时长，0 退出。参数格式与图形界面一致：单个数值（如 100）或英文逗号分隔的区间（如 70,100）；正确率为百分数，时长为秒。

## Docker

仓库自带 Dockerfile（命令行模式，无需本机安装 Python）：

```
docker build -t welearn-helper .
docker run -it --rm welearn-helper
```

# 更新日志

2024.12.18

增加了main.py聚合 并且在 dist/ 有可执行文件可供使用



2025.6.3

更新登录逻辑,修复了无法正常登录的问题



2026.10.13

新增图形界面(多线程不卡顿、可随时停止),保留命令行入口;更新维护者变更为 lxbsdsg



2026.10.14

图形界面改为 Web 版(Flask,自动打开浏览器),移除 tkinter 版;修复登录失败时回调报 NameError 的问题



2026.10.16

更正表述，移除旧版本exe构建，修正一键启动脚本



2026.10.23

切换刷课模式时复用登录会话，无需重新登录



2026.10.25

更正维护者表述，与项目声明一致（YZBRH、lxbsdsg）；运行日志超出任务参数底部时改为日志区内滚动，不再撑高页面

2026.11.1 v0.4.0

发布 Windows 单文件图形界面版 `WelearnHelper.exe`；统一版本号为 0.4.0，新增本地打包脚本与 tag 触发的 Windows 发布流程；修正依赖清单，避免 Python 3 中安装过时 `typing` 包导致无法打包
