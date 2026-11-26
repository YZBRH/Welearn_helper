# 学习时长与正确率合并及回归验证

日期:2026-11-26

相关提交:

- `70effb90a3daa94ad4c1ea621b76883b154b4fd3`
  `fix: unify study task and preserve completion time`
- 当前工作区未提交改动:
  合并刷学习时长与刷正确率模式，支持时长完成后按需提交正确率

## 1. 结论摘要

本次改动没有重新暴露 `70effb9` 修复的问题。

合并模式仍统一调用 `StudyTask.run_study()`。当同时指定学习时长和正确率时，
执行顺序仍然是:

1. 读取 CMI
2. 发送学习心跳并累计真实学习时间
3. 完成指定学习时长
4. 将累计后的 `session_time` / `total_time` 写入 `setscoinfo`
5. 调用 `savescoinfo160928` 保存完成状态和正确率

真实账号单小节验证中，提交后的 `session_time` 和 `total_time` 均为
`00:02:22`，等待 30 秒后再次读取仍保持不变，没有回落为 `0`。

## 2. `70effb9` 修复的问题

### 2.1 问题现象

原正确率模式只关注提交完成状态和正确率，学习时长提交不完整。
正确率请求模板中存在硬编码零时长:

```json
{
  "session_time": "0",
  "total_time": "0"
}
```

服务端可能在后续同步中清空刚写入的学习记录，表现为:

- 正确率显示已提交，但学习时长变成零
- 完成状态保留，学习记录不稳定
- 正确率模式与刷时长模式使用两套请求逻辑，修复难以保持一致

### 2.2 根因

1. 刷时长和刷正确率分别维护自己的请求流程，提交协议不一致。
2. 正确率模式没有先读取当前 CMI，也没有先累计本次学习时间。
3. `setscoinfo` 使用了固定的零时长，而不是本次会话的真实时间。
4. 两套实现重复维护，容易出现一边修复、另一边继续使用旧逻辑的问题。

### 2.3 修复方式

`70effb9` 新增统一的 `StudyTask` 学习内核，并让 `TimeTask`、`AccuracyTask`
共同继承。

关键方法:

- `_read_study_session`: 读取当前 CMI；异常时尝试建立学习会话
- `_keepalive`: 通过 `keepsco_with_getticket_with_updatecmitime` 发送心跳
- `_study_time`: 读取 CMI、累计服务端返回秒数、完成指定学习时长
- `_submit_section`: 学习完成后提交正确率、完成状态和累计时间
- `run_study`: 统一单元遍历、已完成跳过、停止处理和统计

正确率提交前，模板中的时间占位符会使用学习后的 CMI 值:

```python
SCO_DATA_TEMPLATE
    .replace("{session_time}", cmi["session_time"])
    .replace("{total_time}", cmi["total_time"])
```

学习时长通过 `_add_seconds()` 累加，不使用硬编码零值。

### 2.4 同时修复的行为

- 刷时长模式只提交学习记录时，保留已有正确率和完成状态
- 停止发生在学习阶段时，不继续执行 `setscoinfo` / `savescoinfo`
- 两种模式共享请求顺序、心跳、CMI 读取和停止逻辑

## 3. 本次修改的问题与目标

### 3.1 原交互问题

原界面要求用户先选择“刷完成度正确率”或“刷学习时长”：

- 刷时长时不能指定正确率
- 刷正确率时不能自定义学习时长
- 用户需要预先判断模式，操作路径不直观

### 3.2 调整后的交互

合并为一个入口，任务参数包含:

- 每项学习时长，默认 `20,40`
- 可选正确率，支持固定值或区间
- 并行计时小节数，默认 `1`，支持 `1~4`
- 正确率留空时只刷时长，保留原正确率和完成状态

界面不再显示模式单选按钮。

## 4. 本次修改内容

### 4.1 学习任务

`TimeTask.run()` 增加可选 `accuracy` 和 `parallel_workers` 参数:

```python
def run(self, units, param=None, sections=None, accuracy=None,
        parallel_workers=1):
    ...
```

行为分支:

1. 明确填写学习时长:
   - 先学习该时长
   - 处理所选练习，包括已完成小节
   - 正确率非空时，在时长完成后提交正确率
   - 正确率留空时，只提交学习记录并保留已有状态

2. 学习时长留空且填写正确率:
   - 使用原正确率模式的默认学习时长 `20,40`
   - 使用原正确率模式的 `4,10` 秒小节间隔
   - 跳过已完成小节
   - 全部单元模式继续沿用原错误驱动遍历

这里区分“显式时长”和“仅正确率”是有意设计:

- 用户明确刷时长时，不应因为小节已完成就跳过时长累计
- 用户只刷正确率时，保留原正确率模式行为

并行计时实现:

- 先收集需要处理的小节，再使用最多 `parallel_workers` 个工作线程启动计时
- 多个小节的等待时间可以重叠
- 共享的 `tls_client.Session` 由请求锁保护，网络请求仍逐次执行
- 统计结果使用锁保护，停止事件在任务入口和每个等待点检查
- 默认并发为 `1`，保持原顺序行为

### 4.2 Web 界面

- 移除模式单选
- 增加“每项学习时长秒”
- 增加可选“正确率”
- 增加“并发计时小节数”
- 登录时使用 `mode=combined`
- 开始任务时发送 `duration`、`accuracy` 和 `parallel_workers`
- 旧 `mode=accuracy` / `mode=time` API 仍保留兼容

### 4.3 命令行

`main.py` 改为单一入口:

```text
1.刷学习时长并按需写入正确率
0.退出
```

命令行依次询问学习时长和可选正确率:

- 时长直接回车使用 `20,40`
- 正确率直接回车保留原值

### 4.4 结果统计

合并任务结果新增:

- `accuracy_submitted`
- `way1_succeed`
- `way1_failed`
- `way2_succeed`
- `way2_failed`

Web 日志会根据是否提交正确率展示对应统计。

## 5. 与 `70effb9` 的回归关系

### 5.1 没有绕开统一提交路径

合并模式调用链:

```text
Web / CLI / Rust bridge
  -> TimeTask.run()
  -> StudyTask.run_study()
  -> StudyTask._submit_section()
  -> StudyTask._study_time()
  -> CMI 时间更新
  -> setscoinfo
  -> savescoinfo160928
```

本次修改只增加参数选择、UI/API 兼容和结果统计，没有修改
`_study_time()` 或 `_submit_section()` 的核心时间提交逻辑。

### 5.2 原回归测试继续通过

- `tests/test_welearn_accuracy.py::TestCompletionPersistence::test_submits_real_study_time_before_score`
- `tests/test_welearn_accuracy.py::TestCompletionPersistence::test_stop_during_study_does_not_submit`
- `tests/test_welearn_time.py::TestRun::test_preserves_existing_accuracy_and_status`

新增测试覆盖:

- 时长结束后才提交正确率
- 提交包包含累计后的 `session_time` / `total_time`
- 正确率留空不覆盖原值
- 只填正确率时继续使用 `20,40` 默认时长
- 显式时长时处理已完成小节

完整测试结果:

```text
96 passed
```

## 6. 真实账号验证

验证日期:2026-11-26

测试范围:

- 课程:`2831`
- 单元:`Unit 2 Work`
- 小节:`Warm-up > Unit Goals`，第 2 小节
- 参数:学习 1 秒，正确率 100

### 6.1 提交前

```text
session_time: 0
total_time:   0
正确率:       空
状态:         not_attempted
```

### 6.2 实际请求顺序

```text
getscoinfo_v7
keepsco_with_getticket_with_updatecmitime
keepsco_with_getticket_with_updatecmitime
startsco160928
setscoinfo
savescoinfo160928
```

第一次学习心跳中，服务端返回 `seconds=142`。任务使用服务端返回的已学习秒数
进行累计，最终提交时间为 `00:02:22`。

`setscoinfo` 中的关键字段:

```json
{
  "completion_status": "completed",
  "progress_measure": "1",
  "score": {
    "scaled": "100",
    "raw": "100"
  },
  "session_time": "00:02:22",
  "total_time": "00:02:22"
}
```

服务端响应:

```json
{
  "ret": 0,
  "info": "true"
}
```

### 6.3 提交后立即读取

```text
session_time: 00:02:22
total_time:   00:02:22
正确率:       100
状态:         completed
进度:         100%
```

### 6.4 等待 30 秒后再次读取

```text
session_time: 00:02:22
total_time:   00:02:22
正确率:       100
状态:         completed
进度:         100%
```

小节状态变为“已完成”，学习时间显示为 `00:04:28`。

验证结果:

- 学习时间没有在提交后变成零
- 正确率成功写入
- 完成状态和进度成功保留
- 延迟读取结果稳定

## 7. 已知边界与残余风险

1. 服务端首次心跳可能返回大于本次指定时长的累计秒数。
   当前实现使用 `max(服务端返回秒数, 本次时长)` 记录，这是既有逻辑，
   本次修改没有改变。

2. 显式填写时长的合并任务会处理已选中的已完成小节。
   这是“刷时长优先”的有意行为，不代表这些小节会产生零时长正确率记录，
   因为它们仍会先经过 `_study_time()`。

3. 并行计时会缩短多个小节的总体完成时间，可能提高服务端风控命中概率。
   因此默认并发为 `1`，上限为 `4`，不建议长时间高并发运行。

4. 网络请求虽然由锁串行保护，但服务端仍可能将多个并行学习会话识别为异常行为。

5. 真实环境只验证了一个未完成小节。
   已验证核心协议和时间持久化，但未对所有课程、浏览器兼容性和服务端限流
   做全量验证。

6. 当前改动尚未提交到 Git。

## 8. 涉及文件

- `welearn_study.py`
- `welearn_time.py`
- `welearn_accuracy.py`
- `web_gui.py`
- `rust_bridge.py`
- `main.py`
- `tests/test_welearn_time.py`
- `tests/test_web_gui.py`
- `README.md`

## 9. 最终判断

`70effb9` 解决的问题是“正确率提交覆盖学习时长”。本次合并模式只是改变了
参数入口和执行选择，实际学习与提交仍走同一个 `StudyTask` 内核。

代码测试与真实账号验证均表明:

- 当前没有重新暴露零时长覆盖问题
- 合并模式可以在完成学习时长后提交正确率
- 提交后的学习时间能够长期保留
