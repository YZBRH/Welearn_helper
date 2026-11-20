# 顺序任务（多课程队列 · 正确率+时长合并模拟学习）设计规划

日期：2026-11-20
目标版本：`0.5.0`（新增 `queue` 模式）
状态：设计待评审，尚未改代码

---

## 一、需求原文与拆解

> 设置顺序任务，选中多节课之后，按照课程编号顺序进行刷取指定区间的正确率和学习时长，模拟学生学习过程

拆成 5 条可独立验收的行为：

| 编号 | 行为 | 验收方式 |
|---|---|---|
| A | 一次选中 N 门课程，生成队列 | 界面多选后队列卡片数 = 选中数 |
| B | 严格按课程编号升序执行，前一门结束才开始下一门 | 日志顺序 = 编号顺序，无交错 |
| C | 每个小节：先模拟学习 D 秒（D 在区间内随机），再写入 C% 正确率（C 在区间内随机），**一次运行同时生效** | 服务端「学习时长」与「正确率」双双变化，不需要跑两遍 |
| D | 串行：同一时刻只有一个未完成小节在计时 | 日志时间戳不重叠；并发度恒为 1 |
| E | 可随时停止、可跳过当前课程、单课失败不拖垮整个队列 | 停止 2 秒内收尾；跳过后续课程继续 |

**D 和「模拟学生学习过程」是同一个要求的两面。** 现有 `TimeTask` 用 `asyncio.gather` 把所有章节同时开刷（`welearn_time.py:96-98`），20 个小节并发心跳——这不是学生行为。顺序任务必须改成串行 + 区间随机 + 节间休息。

---

## 二、现状拆解

### 2.1 模块职责与规模

| 文件 | 行数 | 职责 | 本次是否改动 |
|---|---|---|---|
| `main.py` | 19 | CLI 菜单聚合，调用两个 `*_run()` | 改（加菜单 3） |
| `welearn_common.py` | 226 | session 工厂、密码加密、参数解析、登录、课程/单元/小节查询、目标归一化 | 改（新增 2 个小函数） |
| `welearn_accuracy.py` | 246 | `AccuracyTask`：只处理未完成章节，双路写入正确率 | 不动（保持回归） |
| `welearn_time.py` | 248 | `TimeTask`：asyncio 并发，心跳模拟时长 | 不动（保持回归） |
| `welearn_flow.py` | 新增 | `FlowTask`：合并流程 + 队列调度 | **新增** |
| `web_gui.py` | 1115 | Flask + 内联 SPA（`INDEX_HTML`）、`AppState`、6 个 API | 改（队列 API + 前端面板） |
| `tests/fakes.py` | 34 | `FakeSession` 按 URL 前缀返回预设响应并记录请求 | 不动 |
| `tests/test_welearn_flow.py` | 新增 | 合并流程与队列的时序测试 | **新增** |

### 2.2 关键数据结构

```python
course  = {"cid": 12345, "per": 60, "name": "大学英语(3)"}      # /ajax/authCourse.aspx?action=gmc
unit    = {"unitname": "Unit 1", "name": "Campus Life", "visible": "true"}
section = {"id": 998877, "location": "Unit 1 > Listening", "iscomplete": "未完成",
           "isvisible": "true", "learntime": 0}                # ?action=scoLeaves&unitidx=N
```

三层索引关系（**这是「课程编号」的唯一可靠来源**）：

- 课程编号 `no` = `fetch_courses()` 返回列表的下标 + 1，程序内**不存在**真正的“课程编号”字段
- 单元参数 `unitidx` 从 0 开始，展示编号从 1 开始（`process_unit(uno-1, uno)`）
- 小节编号 = `scoLeaves` 返回列表下标 + 1，真实标识是 `section["id"]`（`scoid`）

> 推论：队列里的“按课程编号顺序”只能是**快照顺序**。重新登录后 `clist` 顺序可能变化，因此 job 必须存 `cid` 作为唯一真身，`no` 仅用于排序与展示。

### 2.3 两条现有请求时序

**正确率（`welearn_accuracy.py:75-145`）**

```
GET  ?action=scoLeaves&unitidx=K                → 小节列表
  ├ isvisible=false              → 跳过
  ├ iscomplete 含"未"            → 执行写入
  │   POST Ajax/SCO  action=startsco160928
  │   POST Ajax/SCO  action=setscoinfo      data=SCO_DATA_TEMPLATE(crate)
  │   POST Ajax/SCO  action=savescoinfo160928 progress=100 crate=C cstatus=completed
  └ 否则                          → 打印「已完成」跳过
```

**时长（`welearn_time.py:104-166`）**

```
POST Ajax/SCO action=getscoinfo_v7            → comment 内嵌 JSON（crate/cstatus/progress/total_time/session_time）
  └ 若「学习数据不正确」→ POST startsco160928 → 再 getscoinfo_v7 一次
POST Ajax/SCO action=keepsco_..._updatecmitime(session_time, total_time)
for 1..D 秒: sleep(1); 每 60 秒补发一次 keepsco
POST Ajax/SCO action=savescoinfo160928 crate=<读到的值> cstatus=<读到的值> progress=<读到的值>
```

**关键结论：两种模式写的字段是互补的，可以合并，但不能简单叠加两遍。**

- 正确率模式**不写时长**（模板里 `session_time`/`total_time` 硬编码 `"0"`）
- 时长模式**不设正确率**（`crate` 是从 `getscoinfo_v7` 读回来的原值再回写）
- 若「先跑一遍正确率模式，再跑一遍时长模式」：请求量翻倍、两轮遍历、`savescoinfo` 被重复调用，而且中间态（已完成但时长为 0）非常容易被判定异常

### 2.4 四个硬约束（决定了改造点在哪）

| 编号 | 约束 | 代码位置 | 对方案的要求 |
|---|---|---|---|
| **C1** | 任务实例与「单课程」强绑定：`fetch_units(cid)` 会覆写 `self.cid/uid/classid/units` | `welearn_accuracy.py:51-55`、`welearn_time.py:49-57` | 多课程必须显式「切换课程上下文」，且切换后旧的 `units` 快照不可再用 |
| **C2** | `AppState` 是「单任务 / 单模式 / 单课程」：`task`、`mode`、`units`、`sections_map` 各只有一份 | `web_gui.py:29-46` | `sections_map` 的 key 必须从 `unit_no` 改为 `(cid, unit_no)`，否则第二门课会覆盖第一门的小节校验依据 |
| **C3** | 时长是**真实等待**：服务端依据 `keepsco` 心跳推进 `cmitime`，无法压缩 | `welearn_time.py:148-158` | 总耗时 = Σ(每节秒数 + 休息)，数小时级别；UI 必须给预估，并支持断点续跑 |
| **C4** | 模式是二选一互斥电台，且 `_format_result` 按 mode 分支 | `web_gui.py:533-537`、`web_gui.py:77-84` | 新增第三态 `queue`，需同步改 `/api/mode` 工厂映射 + 结果格式化 |

### 2.5 现有代码里三个已知薄弱点（方案要顺手绕开）

1. `"未" in course["iscomplete"]` 判未完成（`welearn_accuracy.py:96`）——语义脆弱，合并流程沿用但集中到一处判定函数
2. `all_units` 是**错误驱动**推进：靠 `scoLeaves` 返回「异常」才停止（`welearn_accuracy.py:83`、`147-157`）——队列里不能这么干，否则每门课都多一次无效探测，且无法显示 `3/8` 进度
3. `savescoinfo160928` 会**覆盖** `crate`——对已完成章节重刷 = 覆盖已有成绩，必须默认跳过

---

## 三、设计方案

### 3.1 总体结构

新增 `welearn_flow.py` 作为第三个控制器，**不改动** `AccuracyTask` / `TimeTask`（保护现有测试与 CLI 行为）。队列调度放在 `web_gui` 的 worker 线程内，用一个 `for` 循环驱动 `FlowTask`。

```
                     ┌─────────────────────────────────────────┐
  web_gui worker  ──▶│ FlowTask.run_queue(jobs)                │
   (单线程串行)      │  for job in sorted(jobs, key=no):       │
                     │      load_course(job.cid)               │
                     │      for unit in scope:                 │
                     │          for section in scope:          │
                     │              _learn_section(...)   ─────┼──▶ 一次提交同时写时长+正确率
                     │      if skip_event: 跳出当前 job        │
                     └─────────────────────────────────────────┘
```

### 3.2 新模块接口

```python
# welearn_flow.py
class FlowTask:
    """队列控制器。与 AccuracyTask/TimeTask 同构,便于 /api/mode 复用会话切换逻辑。"""

    def __init__(self, username, password, on_log=None, stop_event=None,
                 session=None, request_interval=2.0):
        ...

    # ——— 与既有控制器契约一致,供 /api/login、/api/units、/api/sections 直接复用 ———
    def login(self) -> None
    def fetch_courses(self) -> List[dict]
    def fetch_units(self, cid) -> List[dict]      # 内部保存 units 快照
    def fetch_sections(self, unit_index: int) -> List[dict]

    # ——— 队列执行 ———
    def scan(self, jobs) -> List[dict]            # 只读侦察:拉 units + scoLeaves,回填每课小节数
    def run_course(self, job) -> dict             # 单课程,阻塞
    def run_queue(self, jobs, on_progress=None) -> dict
```

`job` 用 **dict**（直接来自浏览器 JSON，避免 dataclass 序列化成本）：

```python
{
  "no": 2,                       # 课程编号,排序与展示用
  "cid": "12345",                # 唯一真身
  "name": "大学英语(3)",
  "scope": {"all": True}         # 或 {"units": [1,3], "sections": {"1": [1,2]}}
  "accuracy": [70, 95],          # int 或 [lo, hi]
  "duration": [40, 90],          # 每小节学习秒数
  "pause": [3, 12],              # 小节之间休息秒数
  "skip_done": True              # 跳过已完成章节(避免覆盖已有成绩)
}
```

### 3.3 单小节合并流程（核心，逐行对齐现有协议）

```python
def _learn_section(self, section, seconds: int, accuracy: int) -> dict:
    common = {"uid": self.uid, "cid": self.cid, "scoid": section["id"]}
    headers = {"Referer": f"https://welearn.sflep.com/student/StudyCourse.aspx"
                          f"?cid={self.cid}&classid={self.classid}&sco={section['id']}"}

    # 1) 打开学习会话,读取服务端已累计的时长(沿用原逻辑与重试路径)
    resp = self.session.post(AJAX_URL, data={**common, "action": "getscoinfo_v7"}, headers=headers)
    if "学习数据不正确" in resp.text:
        self._wait(self.request_interval)
        self.session.post(AJAX_URL, data={**common, "action": "startsco160928"}, headers=headers)
        resp = self.session.post(AJAX_URL, data={**common, "action": "getscoinfo_v7"}, headers=headers)
        if "学习数据不正确" in resp.text:
            raise WelearnError(f"学习数据不正确: {section['location']}")

    cmi = _read_cmi(resp)                    # 空 comment → 全部回退默认值
    session_time, total_time = cmi["session_time"], cmi["total_time"]

    # 2) 首心跳
    self._wait(self.request_interval)
    self._keepalive(common, headers, session_time, total_time)

    # 3) 真实等待 + 每 60 秒补心跳(与 TimeTask 逐字一致)
    for tick in range(1, seconds + 1):
        self._wait(1)                        # stop_event.wait(1),置位即抛 _Stopped
        if tick % self.keepalive_every == 0:
            self._keepalive(common, headers, session_time, total_time)

    # 4) 合并写入:模板里的时长必须回填真实值,正确率写目标值
    data = (SCO_DATA_TEMPLATE
            .replace("{crate}", str(accuracy))
            .replace('"session_time":"0"', f'"session_time":"{session_time}"')
            .replace('"total_time":"0"',   f'"total_time":"{total_time}"'))
    self.session.post(AJAX_URL,
                      data={**common, "action": "setscoinfo", "data": data, "isend": "False"},
                      headers=headers)
    self.session.post(AJAX_URL,
                      data={**common, "action": "savescoinfo160928",
                            "progress": "100", "crate": str(accuracy),
                            "status": "unknown", "cstatus": "completed", "trycount": "0"},
                      headers=headers)
    return {"location": section["location"], "seconds": seconds, "accuracy": accuracy}
```

**这里有一个必须写死的细节（本方案最重要的一个坑，实现时已两次修正）：**

`SCO_DATA_TEMPLATE` 里 `"session_time":"0","total_time":"0"` 是硬编码的（`welearn_accuracy.py:11-17`）。

- **第一次修正**：若直接复用模板，`setscoinfo` 会把时长写成 0 —— 所以必须做字符串回填。
- **第二次修正（实际采用的方案）**：仅回填仍然不够。回填的是 `getscoinfo_v7` 那一刻的旧值，而等待 D 秒期间心跳已经让服务端把时长推进了 D 秒，此时写回旧值等于**回滚 D 秒**。

因此实现里 `FlowTask` **默认不调用 `setscoinfo`**，只调 `savescoinfo160928`：

- 它显式接收 `crate` / `progress` / `cstatus`，是真正落成绩的接口（`TimeTask` 全程也只用它）
- 它不携带任何时间字段，服务端按心跳累计的时长不会被覆盖

`setscoinfo` 通过 `use_setscoinfo` 开关保留（界面「兼容提交 setscoinfo」），仅在实测发现成绩未到账时开启——此时仍必须回填 `session_time`/`total_time`，避免把时长写成 0。


### 3.4 队列模型

```python
# AppState 新增
self.queue: List[dict] = []          # 已构建的 jobs
self.queue_index = 0                 # 当前执行到第几个(0-based)
self.queue_started_at = 0.0
self.queue_eta = 0                   # 侦察阶段算出的预估总秒数
self.skip_event = threading.Event()  # 「跳过当前课程」,每门课开始前 clear
```

排序：`jobs.sort(key=lambda j: j["no"])`。**不做**拖拽排序——用户明确要求"按课程编号"，编号即列表顺序，任何手动排序都会让 B 条验收失去意义。若后续需要，提供"编号 / 完成度升序"两个排序键（V2）。

### 3.5 状态机与 API 变更

新增（都在 `web_gui.create_app()` 内）：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/queue/build` | `{nos:[1,3,4], accuracy, duration, pause, scope}` → 校验、按 no 排序、生成 jobs，返回队列快照 |
| POST | `/api/queue/scan` | 只读侦察：逐课 `fetch_units` + `scoLeaves` 拿小节数 → 返回每课 `{total, todo}` 与 `eta_seconds` |
| POST | `/api/queue/start` | 起 worker 线程跑 `run_queue`；忙检查与线程登记同锁（抄 `api_start` 的写法 `web_gui.py:317-326`） |
| POST | `/api/queue/stop` | 置 `stop_event`（与全局停止同义） |
| POST | `/api/queue/skip` | 置 `skip_event`，只跳出当前课程 |
| GET | `/api/status` | **扩展**返回 `queue: {index, total, current_no, course_no, course_total, unit_no, unit_total, section_no, section_total, elapsed, eta}` |

改造：

- `state.mode` 允许 `"queue"`；`/api/mode` 的工厂映射加一路 `FlowTask`；切换时沿用 `session` 并迁移 `cid/uid/classid/units`（`web_gui.py:217-226` 已有此模式，照抄）
- `state.sections_map` → `{(cid, unit_no): sections}`，`/api/sections` 写入与 `/api/start` 校验同步改（否则两门课的单元 1 会互相覆盖）
- `_format_result(mode, result)` 加 `queue` 分支：逐课一行「课程 2/5 · 完成 12 节 · 跳过 3 节 · 失败 0 · 用时 18:20」
- `/api/start` **保持不变**（单课程双模式仍然可用），队列走独立入口，互不干扰

### 3.6 前端改动（`INDEX_HTML` 内）

新增第 5 个面板「顺序任务」（放在「任务参数」下方，并让模式电台变三选一）：

```
┌ 5 顺序任务 ────────────────────────────┐
│ ☑ NO.1 完成度 12%  大学英语(3)          │
│ ☐ NO.2 完成度 88%  视听说(2)            │
│ ☑ NO.3 完成度 40%  学术写作             │
│ 正确率 [70,95]  每节时长 [40,90]        │
│ 节间休息 [3,12]  ☑ 跳过已完成章节        │
│ 范围：● 整门课全部单元  ○ 指定单元…      │
│              [生成队列] [预估耗时]       │
├ 队列 ──────────────────────────────────┤
│ 1 大学英语(3)  全部单元  128 节  待执行  │
│ 2 学术写作     全部单元   46 节  待执行  │
│ 预估总时长 约 3 小时 12 分（128+46 节）  │
└────────────────────────────────────────┘
```

运行条（`.runbar`，`web_gui.py:570-579`）扩展为队列进度：

```
目标  第 2/2 门 · 单元 3/8 · 小节 2/5 · 已用 41:20 · 剩余约 2:31:00
                                        [跳过当前课程] [开始] [停止]
```

状态轮询已有 1 秒 `/api/status`（`web_gui.py:1065-1082`），队列进度搭这个车，不新增轮询。

### 3.7 耗时模型（必须在界面上说清楚）

```
单节耗时 = duration_avg + 4 × request_interval(2s) + 小节内抖动
单课耗时 = Σ(单节) + (节数 − 1) × pause_avg
总耗时   = Σ(单课)

示例：2 门课 × 87 节，时长 40~90s（均值 65），休息 3~12s（均值 7.5）
     = 174 × (65 + 8 + 7.5) ≈ 14,000 秒 ≈ 3 小时 53 分
```

**这是本功能最大的体验风险**：用户点"开始"时以为几分钟，实际半天。因此：

1. `scan` 阶段就给出精确节数（只读，几秒钟）
2. 运行中每秒更新「已用 / 剩余」
3. 首次使用弹一次提示：「顺序任务按真实时间推进，预计 X 小时 Y 分，期间浏览器可关闭但本程序窗口不可关」

### 3.8 停止 / 跳过 / 失败 三种语义

| 动作 | 实现 | 收尾行为 |
|---|---|---|
| **停止** | 置 `stop_event` | 当前小节**立即中断且不调 `savescoinfo`**（留下半截数据比不刷更异常）；已完成小节保留；队列快照落盘 |
| **跳过当前课程** | 置 `skip_event` | 当前小节跑完（或立即中断亦可，建议跑完以免半截），跳出该 job，`status=skipped`，继续下一门 |
| **单课失败** | `run_course` 抛 `WelearnError` 被 `run_queue` 捕获 | 记 `status=failed` + `error`，默认**继续下一门**；`on_error="halt"` 时停队列 |
| **登录过期** | 响应体含登录页特征 / `ret` 非 0 连续 3 次 | 抛 `LoginError` → 队列暂停，前端提示「请重新登录，登录后可从第 N 门续跑」（V2） |

---

## 四、分阶段任务清单

### P0 · 核心流程（约 0.5 天）——先让「一门课一次跑完时长+正确率」成立

- [ ] **T1** `welearn_common.py` 新增两个小工具
  - `read_cmi(resp) -> dict`：`comment` 内嵌 JSON 解析，缺字段回退 `{'scaled':'','completion_status':'unknown','progress_measure':'0','total_time':'0','session_time':'0'}`（先把 `welearn_time.py:126-139` 的解析逻辑抽出来，再让 `FlowTask` 调用；`TimeTask` 暂不动，P3 再回收）
  - `is_unfinished(section) -> bool` / `is_visible(section) -> bool`：集中 `"未" in ...`、`== 'false'` 两处脆弱判定
  - 测试：`tests/test_welearn_common.py` 加 3 个用例（空 comment、缺 cmi、正常 cmi）

- [ ] **T2** 新增 `welearn_flow.py`
  - `FlowTask.__init__/login/fetch_courses/fetch_units/fetch_sections`（`fetch_units` 复制 `TimeTask` 的「先请求 /2019/ 页再解析 uid」序列，保持协议一致）
  - `_wait(sec)` = `stop_event.wait(sec)` 抛 `_Stopped`；`_keepalive(...)` 封装 `keepsco_with_getticket_with_updatecmitime`
  - `_learn_section(section, seconds, accuracy)`：见 3.3
  - `run_course(job)`：遍历 scope，跳过 `visible=false`、按 `skip_done` 跳过已完成、节间 `_wait(rand(pause))`、写日志前缀 `[课程3/5][单元2/8][小节4/12]`
  - 返回 `{"ok":bool, "total":int, "done":int, "skipped":int, "failed":int, "errors":[str], "elapsed":float}`
  - **不引入 asyncio**：同步实现，`stop_event.wait()` 天然可中断，比 `asyncio.run` 更好控

- [ ] **T3** `tests/test_welearn_flow.py`（关键，先写测试）
  - `test_single_section_writes_time_and_accuracy_together`：FakeSession 断言请求序列为 `getscoinfo_v7 → keepsco → savescoinfo160928`，且最后一次 `savescoinfo` 的 `crate == 目标值`；若走 `setscoinfo`，其 `data` 中 `total_time` **不等于** `"0"`
  - `test_serial_only_one_section_at_a_time`：给 FakeSession 打时间戳，断言相邻小节的时间区间不重叠、且总耗时 ≈ Σ 秒数
  - `test_stop_interrupts_within_two_seconds`：`duration=600`，1 秒后 set stop，断言 `elapsed < 3` 且未发 `savescoinfo`
  - `test_skip_done_sections_not_overwritten`：`iscomplete="completed"` 的章节不产生任何 POST
  - `test_accuracy_and_duration_sampled_per_section`：固定 seed，断言 12 个小节的 `crate`/`seconds` 不全相同且都落在区间内

- [ ] **T4** `main.py` 加菜单 `3.顺序任务（多课程）`
  - 交互：多选课程编号（`1,3,4`）→ 正确率区间 → 时长区间 → 确认预估 → 执行
  - 复用 `parse_numeric_param`

**P0 验收**：单门课跑 `python -u main.py` 选 3，用极小参数（正确率 70,75、时长 5,8）验证服务端「时长与正确率同时变化」。

### P1 · 队列与界面（约 1 天）

- [ ] **T5** `AppState` 扩展：`queue / queue_index / queue_started_at / queue_eta / skip_event`；`sections_map` 改 `(cid, unit_no)` 键（同步改 `/api/sections` 与 `/api/start`）
- [ ] **T6** 队列 API 五个端点（3.5），全部沿用现有「忙检查 + `_fetch_begin/_fetch_end` + 同锁登记线程」范式
- [ ] **T7** `/api/mode` 支持 `"queue"`；`_format_result` 加队列摘要
- [ ] **T8** 前端：模式电台三选一、课程多选列表、参数输入、队列卡片、预估显示、进度条
- [ ] **T9** `tests/test_web_gui.py` 扩充：build 后 `no` 升序、参数非法 400、运行中再 start 409、queue 快照字段齐全

### P2 · 体验与稳健（约 0.5 天）

- [ ] **T10** `scan` 侦察阶段 + 精确 ETA；运行中剩余时间按「已完成节数 × 单节均值」动态校正
- [ ] **T11** 失败策略开关（继续 / 立即停）；单节重试 1 次（沿用 `getscoinfo_v7` 的「学习数据不正确」重试路径）
- [ ] **T12** 日志分组与缩进：`[课程2/5][单元3/8][小节2/5] 已学 62s 正确率 88%`
- [ ] **T13** 停止后队列快照保留在 `state.queue`，界面可「继续未完成部分」（不落盘，仅内存）

### P3 · 进阶（约 1 天，可延后）

- [ ] **T14** 断点续跑：`_state/queue-<账号hash>.json`，启动时提示「检测到未完成队列 2/5，是否继续」
- [ ] **T15** 队列导入/导出 JSON（多账号、多学期复用）
- [ ] **T16** 随机化策略：正确率「爬升曲线」（前 30% 低分、后段高分）、时长正态分布而非均匀分布——更像真人
- [ ] **T17** 把 `TimeTask` 的 cmi 解析也切到 `welearn_common.read_cmi`，消除重复（纯重构，需保证现有测试全绿）

---

## 五、测试计划

**单测（pytest，`python -m pytest tests/ -v`）**

| 文件 | 用例 | 断言重点 |
|---|---|---|
| `test_welearn_flow.py` | 合并写入 | 一次 `run_course` 同时产生时长心跳与正确率写入；`setscoinfo.total_time != "0"` |
| | 串行性 | 相邻小节时间窗口不重叠 |
| | 停止 | < 3 秒收尾，不发 `savescoinfo` |
| | 跳过已完成 | `completed` 章节零 POST |
| | 区间取样 | 值落在区间内、非恒定 |
| | 单元越界 | `scope.units` 超出 `len(units)` 只跳过不抛 |
| `test_web_gui.py` | 队列 build / scan / start / stop / skip | 排序、409、参数校验、快照字段 |
| **回归** | `test_welearn_time.py`、`test_welearn_accuracy.py`、`test_welearn_common*.py` | **必须全绿且零修改**——证明既有模式未被破坏 |

**手工验收（真实账号，先小参数）**

1. 两门课各选 1 个单元 2 个小节，正确率 `70,75`、时长 `5,8` → 服务端时长与正确率同时变化
2. 观察日志时间戳：任意两行「即将完成」之间间隔 ≥ 时长下限，无并发交错
3. 运行中点「跳过当前课程」→ 第 1 门标 `skipped`，第 2 门在 10 秒内开始
4. 运行中点「停止」→ 3 秒内状态回「待命」，按钮恢复，已完成小节成绩保留
5. 队列跑到一半关掉浏览器 → 任务继续（任务在 Flask 进程里，不依赖前端）
6. 单模式（非队列）功能全量回归：正确率、时长、单元多选、小节多选、刷新课程、模式切换

---

## 六、风险与非目标

**风险**

| 等级 | 风险 | 缓解 |
|---|---|---|
| 高 | `setscoinfo` 把模拟时长清零，用户以为白跑 | T2 必须回填 `session_time`/`total_time`；T3 有专项断言；实测优先验证 |
| 高 | 覆盖已有成绩（`savescoinfo` 直接改 `crate`） | `skip_done` 默认 `True`，界面明示 |
| 中 | 总耗时数小时，用户中途关闭进程 | `scan` 阶段给精确 ETA + 首启提示 + P3 断点续跑 |
| 中 | 多课程下 `state.units`/`sections_map` 串味 | 队列以 job 内 `scope` 快照为准，运行期不回读 `state.units` |
| 中 | 高正确率 + 零间隔 + 全课程一次性刷完，行为特征明显 | 串行 + 区间随机 + 节间休息；README 建议正确率上限 95、不要 100 |
| 低 | 长队列跨小时导致登录过期 | 连续 3 次 `ret != 0` 判为会话失效，暂停并提示（P3 续跑） |

**非目标（本次不做）**

- 多账号并发（现有设计是单 session 单 worker，改动成本远超收益）
- 后台常驻/定时任务（`automation` 不属于本项目范畴）
- 突破服务端风控（本方案只做"像人"的节奏，不做协议层绕过）
- 重写 `AccuracyTask` / `TimeTask`（P0–P2 一行不动，避免回归）
- 新增第三方依赖（`requirement.txt` 不变）

---

## 七、验收后修订（2026-11-20 第二轮）

实机跑过后发现两处问题，均已修复并加了回归断言：

### 修订 1：顺序刷取必须保留「单元与小节」窗口

初版为缩短路径，让 queue 模式忽略单元/小节勾选（整门课遍历），并把单元树禁用到灰显。实际使用中这是错的——用户需要在队列里指定范围。

修改：

- `applyModeUI()` 不再隐藏 `courseSel`、不再禁用 `allUnits`、不再灰显 `#unitTree`；下拉框改为「查看单元结构」用途
- `api_queue_start` / `api_queue_preview` 接收 `units` + `sections`，用 `welearn_common.normalize_targets` 归一化后**写进每个 job**
- `FlowTask._run_course_inner` 按 job 的 `units` 取 `targets = [u-1]`，`_run_sections` 增加 `wanted` 参数按小节编号过滤
- 单元编号超出某门课程时只跳过超出部分（记一条日志），全部越界则该课程 `status=skipped` 继续下一门
- `queueSelectionSummary()` 与 `startQueue()` 带上范围，摘要显示「N 个单元 / M 个小节」

### 修订 2：账号/密码输入框错位

根因是上一轮为「任务参数」面板加间距时写成了后代选择器：

```css
.p-body .field + .field { margin-top: 10px; }   /* 错:同时命中 .grid-2 里的第二个 field */
.p-body > .field + .field { margin-top: 10px; } /* 对:只作用于直接子级堆叠字段 */
```

`.grid-2` 里两个 `.field` 是相邻兄弟，第二个（密码）因此被多推 10px，与账号列错位。改为子选择器后，会话面板不受影响，任务参数的三个输入框仍有间距。

### 新增回归覆盖

| 层 | 断言 |
|---|---|
| 单测 | 只请求勾选单元（`unitidx` 序列）；只写入勾选小节（`scoid`）；单元越界 → `skipped`；默认全部单元 |
| 接口 | `units`/`sections` 写入 job；默认 `units=0`；空单元列表 400 |
| DOM（jsdom） | 切顺序刷取后时长框/多选列表出现、`allUnits` 未被禁用、单元树未灰显；新选择器不命中密码字段、旧选择器命中（复现根因）；完整走登录→勾单元→勾小节→开始，断言请求体带 `units=[1]`、`sections={"1":[1]}` |

---

## 八、验收后修订 2（2026-11-20 第三轮）

### 修订 3：删除队列课程勾选列表，选择模型收敛为「下拉框 + 单元树」

多选列表与下拉框并存造成两处选课入口，易误操作。改为：

- 删除 `#coursePick` / `#coursePickTip` / `renderCoursePick()` 及相关 CSS
- `pickedCids()` 直接取 `#courseSel` 的当前值（数组仍保留，后端多课程能力不变）
- 单元/小节勾选继续由 `#unitTree` 决定，写进 job 的 `units` / `sections`
- 运行条摘要改为「顺序刷取 · 课程名 · N 个单元 / M 个小节 · 正确率 · 时长」

### 修订 4：顺序刷取没有任何提示，需 10 秒一次心跳

与 `TimeTask` 的 `_heartbeat` 对齐，新增 `FlowTask._beat()`：

```
[心跳] 本小节 10/45 秒 · 本课程已学 00:10 · 本次任务累计 00:10
```

按墙钟计时（`time.time()` 差值 ≥ `HEARTBEAT_INTERVAL=10`），因此跨小节也能保持 10 秒节奏，短小节不会漏报。

### 修订 5（功能缺陷）：只刷了时长，正确率没落库

**现象**：跑完后小节显示「用时」，但正确率没变。

**根因**：上一轮为避免 `setscoinfo` 回滚时长，默认只调 `savescoinfo160928`。但该接口只是收尾接口，服务端需要 `setscoinfo` 先把 `score.scaled` 写进 cmi，否则 `crate` 参数不生效——`AccuracyTask` 的「方式1」正是这一步。

**修复**：直接套用 `AccuracyTask` 的三步，不做其它改动：

```
getscoinfo_v7（读最新累计时长）
→ keepsco 心跳等待 D 秒
→ getscoinfo_v7（再取一次最新值）
→ startsco160928
→ setscoinfo（模板 crate=C，session_time/total_time 回填最新值）
→ savescoinfo160928（progress=100, crate=C, cstatus=completed）
```

唯一的保留项：模板里 `session_time`/`total_time` 仍必须回填，不能用模板里的 `"0"`，否则时长被清零。多出来的那次 `getscoinfo_v7` 就是为了拿到「已经包含刚刷出的 D 秒」的值。

同时删除 `use_setscoinfo` 开关与界面上「兼容提交 setscoinfo」勾选项——既然默认就要走方式1，开关没有意义。

### 新增回归覆盖（第二轮）

| 层 | 断言 |
|---|---|
| 单测 | `startsco160928 → setscoinfo → savescoinfo160928` 顺序；`setscoinfo` 的 data 含 `"scaled":"88"` 且 `total_time != "0"`；心跳按 10 秒输出且含三段信息 |
| 接口 | 页面不再含 `#coursePick`；`units`/`sections` 进 job；`setscoinfo` 字段不再出现在请求体 |
| DOM（jsdom） | 列表已移除、下拉框保持可见且取当前值、单元可勾选、运行条显示课程名、请求体 `cids=["11"] units=[1] sections={"1":[1]}` |

---

## 九、一句话总结

现有程序是「单课程 × 单模式」的两个独立脚本，时长和正确率走两套互不相干的时序。这个功能新增第三个控制器 `FlowTask`：把心跳计时与正确率写入合并进同一个小节流程（默认只调 `savescoinfo160928`，避免 `setscoinfo` 回滚时长），再用串行 `for` 循环按 `no` 升序串起多门课；`stop_event` 管整体停止、`skip_event` 管跳课；每门课的刷取范围由 job 内的 `units`/`sections` 快照决定，前端沿用同一套单元树。
