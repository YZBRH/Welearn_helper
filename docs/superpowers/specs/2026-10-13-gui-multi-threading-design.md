# Welearn_helper 图形界面(多线程)设计文档

日期:2026-10-13
分支:`feature/gui`(自 `main` 切出)

## 背景与目标

Welearn_helper 目前是纯命令行交互工具(`main.py` 聚合菜单,`welearn_time.py` /
`welearn_accuracy.py` 两个刷课模块内散布 `input()`/`print()`)。目标:

1. 新增 tkinter 图形操作界面,刷课任务在后台线程运行,GUI 不卡死、日志实时滚动、可随时停止
2. 保留原命令行入口,行为不变
3. 启动界面及 README 中「更新维护者」改为 lxbsdsg(保留原作者 Avenshy & SSmJaE 署名)

## 需求确认(已与维护者对齐)

- 「多线程」指界面不卡死 + 可停止,**不含**多账号并发
- GUI 框架:tkinter(Python 标准库,无新增依赖)
- 双入口:`main.py` CLI 保留,新增 `gui.py`
- 作者展示:`lxbsdsg` + `https://github.com/lxbsdsg/Welearn_helper`

## 架构

### 模块结构

```
welearn_common.py   (新) 共享逻辑:登录(现两文件重复)、generate_cipher_text、
                        课程列表/uid/classid/单元查询
welearn_time.py     (重构) 刷时长核心 → TimeTask 控制器类;保留 welearn_time_run() CLI 包装
welearn_accuracy.py (重构) 刷正确率核心 → AccuracyTask 控制器类;保留 welearn_accuracy_run() 包装
main.py             (微调) 菜单不变,横幅「更新维护者」改为 lxbsdsg
gui.py              (新) tkinter 图形界面入口
一键启动.bat         (改) 启动 gui.py
命令行启动.bat       (新) 原 python -u main.py 行为
```

### 控制器接口(两模式同构)

```python
class TimeTask / AccuracyTask:
    def __init__(self, username, password, on_log: Callable[[str], None],
                 stop_event: threading.Event)
    def login(self) -> None                  # 失败抛 LoginError(替代原 exit(0))
    def fetch_courses(self) -> List[dict]    # [{cid, per, name}]
    def fetch_units(self, cid) -> List[dict] # [{unitname, name, visible}]
    def run(self, unit_index: int, param) -> dict
        # 阻塞执行刷课(调用方负责放入工作线程);返回统计 {成功数, 失败数, 错误列表}
        # param: time 模式为 int 或 (min,max);accuracy 模式为正确率 int 或 (min,max)
```

重构要点:

- 原模块级全局(`session`、`cid`、`uid`、`courseInfo`、`targetTime`、`errors` 等)收编为实例属性,每个任务独立 `tls_client.Session`
- 原 `exit(0)` 错误路径(密码错误、登录异常)改为抛 `LoginError`/`WelearnError`,由 CLI 或 GUI 决定如何呈现
- `welearn_accuracy.py` 中 `eval(inputcrate)` 改为按逗号分割的安全解析(行为等价,仅去掉 eval)
- `welearn_time.py` 的 asyncio(`simulate`/`heartbeat`)保留,`asyncio.run()` 在工作线程内执行,每个线程独立事件循环

### 线程模型

- GUI 为单线程 tkinter;任务在 `threading.Thread(daemon=True)` 中执行
- 日志:控制器内所有 `print` 改为 `on_log(text)` 回调;GUI 侧回调仅做 `queue.Queue.put`,主循环 `after(100ms)` 轮询取出写入日志区 —— 回调永远不在工作线程里碰控件
- 停止:`threading.Event` 传入控制器,在 `simulate()` 每秒等待循环、心跳循环、单元迭代、登录重试等长循环处检查;置位后任务尽快收尾并返回已完成统计
- 关窗时有任务运行:弹确认框 → 置停止事件 → 短暂 join → 销毁

### GUI 布局(tkinter + ttk,单窗口)

```
┌────────────────────────────────────────────┐
│ 横幅: Created By Avenshy & SSmJaE          │
│       更新维护者 github.com/lxbsdsg/...     │
│ [账号____] [密码____] (模式◎正确率 ○时长) [登录] │
│ ┌课程列表(Treeview: 完成度/名称)┐ [刷新]     │
│ ┌单元列表(Treeview: 序号/名称/开放)┐        │
│ 单元序号[0=全部]__ 参数[__](时长秒或正确率%)  │
│ [开始] [停止]              状态栏: 待命/运行中 │
│ ┌日志区 ScrolledText 只读,实时滚动──────────┐│
└────────────────────────────────────────────┘
```

交互流:登录 → 自动加载课程 → 选中课程自动加载单元 → 填参数 → 开始。
运行中禁用登录/开始,启用停止;结束后恢复。

### 作者信息更新

- `main.py` 横幅第 7 行 → `更新维护者github https://github.com/lxbsdsg/Welearn_helper/`
- `gui.py` 横幅同样格式
- `README.md`:声明区加「现由 [lxbsdsg](https://github.com/lxbsdsg/Welearn_helper) 更新维护」;更新日志补 `2026.10.13 新增图形界面(多线程不卡顿、可停止),保留命令行入口`
- 原作者署名(Avenshy、SSmJaE、GPLv3 声明)全部保留

## 错误处理

- 登录失败/密码错误:`LoginError` → GUI 弹窗 + 日志记录;CLI 保持原提示文案
- 课程列表为空:提示可能登录错误或无课程(沿用原文案)
- 网络异常:控制器内捕获、记日志、按原重试逻辑处理;停止事件优先于重试
- GUI 侧任何控制器异常捕获后写入日志区并恢复按钮状态,不崩溃

## 测试与验收

项目无测试框架,采用手工验收:

1. `python gui.py` 启动,窗口布局完整、横幅作者信息正确
2. 假账号登录 → 明确报错弹窗/日志,GUI 不崩溃、按钮恢复
3. 选模式/课程/单元/参数的状态流转正确(无课程时控件禁用合理)
4. 任务运行中:日志滚动、GUI 可拖动不卡死、停止按钮能在数秒内停止任务
5. `python main.py` CLI 流程与原来一致(提示文案、刷课行为)
6. 两个 .bat 分别启动 GUI 与 CLI
7. 真实账号刷课由维护者实测

## 非目标

- 多账号并发刷课
- PyInstaller 重新打包 dist/ 可执行文件(由维护者需要时自行执行)
- 新增第三方依赖(requirement.txt 不变,tkinter 为标准库)
- 重写请求协议/绕过反爬等逻辑改动(仅做结构重构,请求序列保持原样)
