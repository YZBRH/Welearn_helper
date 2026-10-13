# Welearn_helper 图形界面(多线程)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Welearn_helper 增加 tkinter 图形界面(任务在后台线程运行、可随时停止、日志实时滚动),保留原命令行入口,并把「更新维护者」改为 lxbsdsg。

**Architecture:** 把两个刷课模块中散布 `input()`/`print()` 的流程重构为无界面控制器类(`TimeTask`/`AccuracyTask`),共享的登录与课程查询提取到 `welearn_common.py`;CLI 与 GUI 作为两套薄入口驱动同一套核心。GUI 侧任务跑在 daemon 线程,日志经 `queue.Queue` 回传、`after(100ms)` 轮询写盘,`threading.Event` 作停止开关。

**Tech Stack:** Python 3.8+(标准库 tkinter / threading / queue / asyncio / tls_client);pytest 仅用于开发期测试。

**Spec:** `docs/superpowers/specs/2026-10-13-gui-multi-threading-design.md`

## Global Constraints

- 不新增运行时第三方依赖:`requirement.txt` 保持不变(tkinter 为标准库;pytest 仅开发环境安装)
- Python 3.8+ 兼容语法,沿用现有代码的旧式 typing 风格(`List`、`Tuple`、`Callable` 等)
- 原作者署名(Avenshy、SSmJaE)与 GPLv3 声明全部保留;仅把「更新维护者」相关字样改为 lxbsdsg
- 网络请求的 URL、参数、顺序与原实现逐字一致 —— 本计划是结构重构,不改协议
- CLI 提示文案与原版逐字一致(日志行合并输出除外,见 Task 3 说明)
- 分支:`feature/gui`;提交信息用 conventional commits(`feat:`/`test:`/`docs:`/`chore:` + 中文描述)
- 测试命令一律在仓库根目录执行:`python -m pytest tests/... -v`

## Review Focus

(spec 未显式覆盖、但最容易咬到真实用户的五类输入/故障 —— 每条已钉到对应任务的测试里)

1. 参数输入非法(`abc`、`10,30,40`、`10,`)→ 抛 `ValueError` 明确提示,不崩溃、不走 `eval` → Task 1 测试
2. 密码含非 ASCII 字符(中文等)→ cipher 的 utf-8 编码/解码往返正确 → Task 1 测试
3. 登录重试循环(`code == -1`)期间用户点停止 → 抛 `WelearnError("已停止")` 中断,不死循环 → Task 2 测试
4. 课程列表为空 → `WelearnError('发生错误!!!可能是登录错误或没有课程!!!')`,不抛裸异常 → Task 2 测试
5. 任务运行中点停止/关窗 → 数秒内收尾返回统计,GUI/线程不崩 → Task 3 停止测试 + Task 7 手工验收

---

### Task 1: 测试基建 + `welearn_common.py` 基础部分(异常/加密/参数解析)

**Files:**
- Create: `welearn_common.py`
- Create: `tests/test_welearn_common.py`

**Interfaces:**
- Consumes: 无(首任务)
- Produces(后续任务按此引用):
  - `class LoginError(Exception)`、`class WelearnError(Exception)`
  - `generate_cipher_text(password: str, now_ms: Optional[int] = None) -> Tuple[str, int]`
  - `parse_numeric_param(text: str) -> Union[int, Tuple[int, int]]`(非法输入抛 `ValueError`)

- [ ] **Step 1: 安装 pytest**

```bash
python -m pip install pytest
```

- [ ] **Step 2: 写失败测试**

`tests/test_welearn_common.py`:

```python
import base64

import pytest

from welearn_common import generate_cipher_text, parse_numeric_param


class TestGenerateCipherText:
    def test_roundtrip_with_fixed_now(self):
        E, T1 = generate_cipher_text("Passw0rd!", now_ms=1700000000000)
        decoded = base64.b64decode(E).decode('utf-8')
        t1_str, hex_part = decoded.split('*', 1)
        assert int(t1_str) == T1
        assert bytes.fromhex(hex_part) == "Passw0rd!".encode('utf-8')

    def test_t1_within_hundred_of_t0(self):
        T0 = 1700000000000
        _, T1 = generate_cipher_text("whatever", now_ms=T0)
        assert T0 // 100 * 100 <= T1 <= T0 // 100 * 100 + 99

    def test_deterministic_for_same_now(self):
        assert (generate_cipher_text("pw", now_ms=1234567890123)
                == generate_cipher_text("pw", now_ms=1234567890123))

    def test_non_ascii_password(self):
        pwd = "密码Abc123"
        E, T1 = generate_cipher_text(pwd, now_ms=999999999999)
        decoded = base64.b64decode(E).decode('utf-8')
        _, hex_part = decoded.split('*', 1)
        assert bytes.fromhex(hex_part) == pwd.encode('utf-8')


class TestParseNumericParam:
    def test_single_value(self):
        assert parse_numeric_param("30") == 30

    def test_single_value_with_spaces(self):
        assert parse_numeric_param("  100 ") == 100

    def test_range_value(self):
        assert parse_numeric_param("10,30") == (10, 30)

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_numeric_param("abc")

    def test_rejects_three_parts(self):
        with pytest.raises(ValueError):
            parse_numeric_param("10,30,40")

    def test_rejects_trailing_comma(self):
        with pytest.raises(ValueError):
            parse_numeric_param("10,")

    def test_rejects_inverted_range(self):
        with pytest.raises(ValueError):
            parse_numeric_param("30,10")
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_welearn_common.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'welearn_common'`

- [ ] **Step 4: 实现 `welearn_common.py` 基础部分**

```python
"""Welearn_helper 共享核心:异常、登录加密、参数解析、登录与课程查询(CLI 与 GUI 共用)。"""
import base64
import re
import time
from typing import Callable, List, Optional, Tuple, Union

import tls_client

AJAX_BASE = "https://welearn.sflep.com"


class LoginError(Exception):
    """账号密码错误或登录流程失败"""


class WelearnError(Exception):
    """业务流程异常(响应异常、空课程列表、被用户停止等)"""


def new_session() -> tls_client.Session:
    return tls_client.Session(client_identifier="chrome_120", debug=False)


def to_hex_byte_array(byte_array) -> str:
    return ''.join([f'{byte:02x}' for byte in byte_array])


def generate_cipher_text(password: str, now_ms: Optional[int] = None) -> Tuple[str, int]:
    # 获取当前时间戳(毫秒级);不清楚原因,网站和本地间有延迟,需手动补齐延迟
    T0 = int(round(now_ms if now_ms is not None else time.time() * 1000))
    # 模拟TextEncoder.encode
    P = password.encode('utf-8')
    V = (T0 >> 16) & 0xFF
    for byte in P:
        V ^= byte
    remainder = V % 100
    T1 = int((T0 / 100) * 100 + remainder)
    P1 = to_hex_byte_array(P)
    S = f"{T1}*" + P1
    S_encoded = S.encode('utf-8')
    # 模拟btoa
    E = base64.b64encode(S_encoded).decode('utf-8')
    return E, T1


def parse_numeric_param(text: str) -> Union[int, Tuple[int, int]]:
    """解析 '30' -> 30;'10,30' -> (10, 30);其余抛 ValueError(替代原 eval 用法)。"""
    text = text.strip()
    if ',' in text:
        parts = text.split(',')
        if len(parts) != 2:
            raise ValueError(f"格式异常: {text!r}(区间格式为 最小值,最大值)")
        lo, hi = int(parts[0]), int(parts[1])
        if lo > hi:
            raise ValueError(f"格式异常: {text!r}(下限大于上限)")
        return lo, hi
    return int(text)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_welearn_common.py -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add welearn_common.py tests/test_welearn_common.py
git commit -m "feat: 新增 welearn_common 基础模块(异常/加密/参数解析)及测试"
```

---

### Task 2: `welearn_common.py` 网络函数(登录/课程查询)+ FakeSession 测试

**Files:**
- Modify: `welearn_common.py`(文件末尾追加网络函数)
- Create: `tests/fakes.py`
- Create: `tests/test_welearn_common_net.py`

**Interfaces:**
- Consumes: Task 1 的异常类
- Produces(后续任务按此引用):
  - `login(session, user: str, pwd: str, on_log: Callable[[str], None] = 无操作, stop_event: Optional[threading.Event] = None) -> None`(失败抛 `LoginError`,被停止抛 `WelearnError`)
  - `fetch_courses(session) -> List[dict]`(每项含 `cid`/`per`/`name`;空列表抛 `WelearnError`)
  - `get_course_ids(session, cid: str) -> Tuple[str, str]`(返回 `(uid, classid)`)
  - `fetch_units(session, cid: str, uid: str) -> List[dict]`
  - `tests/fakes.py` 的 `FakeResponse`、`FakeSession`(Task 3/4 复用)

- [ ] **Step 1: 写失败测试**

`tests/fakes.py`:

```python
"""测试用伪 tls_client.Session:按 URL 前缀返回预设响应,并记录所有请求。"""


class FakeResponse:
    def __init__(self, json_data=None, text="", headers=None, status_code=200):
        self._json = json_data
        self.text = text
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json data configured")
        return self._json


class FakeSession:
    def __init__(self):
        self.requests = []   # [(method, url, data), ...]
        self.routes = {}     # {url_prefix: FakeResponse 或 callable(url, data) -> FakeResponse}

    def get(self, url, **kwargs):
        return self._handle("GET", url, None)

    def post(self, url, data=None, **kwargs):
        return self._handle("POST", url, data)

    def _handle(self, method, url, data):
        self.requests.append((method, url, data))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp(url, data) if callable(resp) else resp
        raise AssertionError(f"未预置响应: {method} {url}")
```

`tests/test_welearn_common_net.py`:

```python
import threading

import pytest

from tests.fakes import FakeResponse, FakeSession
import welearn_common
from welearn_common import LoginError, WelearnError

# 顺序经过 split("&") 后 index 4 是 code_challenge、index 6 是 state
LOC = ("https://sso.sflep.com/idsvr/connect/authorize?client_id=welearn_web"
       "&redirect_uri=https%3A%2F%2Fwelearn.sflep.com%2Fsignin-sflep"
       "&response_type=code&scope=openid&code_challenge=TEST_CHALLENGE"
       "&code_challenge_method=S256&state=TEST_STATE&x-client-SKU=ID_NET472")


def make_login_session(code=0):
    fake = FakeSession()
    fake.routes["https://welearn.sflep.com/user/prelogin.aspx"] = \
        FakeResponse(headers={"Location": LOC})
    fake.routes["https://sso.sflep.com/idsvr/account/login"] = \
        FakeResponse(json_data={"code": code, "data": "/idsvr/next"})
    fake.routes["https://sso.sflep.com/idsvr/next"] = FakeResponse(text="ok")
    return fake


class TestLogin:
    def test_success(self):
        fake = make_login_session(code=0)
        logs = []
        welearn_common.login(fake, "user", "pw", on_log=logs.append)
        assert any("登录成功" in m for m in logs)
        assert any(m[0] == "POST" and "account/login" in m[1] for m in fake.requests)

    def test_wrong_password_raises_login_error(self):
        fake = make_login_session(code=1)
        with pytest.raises(LoginError):
            welearn_common.login(fake, "user", "bad")

    def test_stop_event_interrupts_retry_loop(self):
        fake = make_login_session(code=-1)   # code=-1 会一直重试
        stop = threading.Event()
        stop.set()
        with pytest.raises(WelearnError):
            welearn_common.login(fake, "user", "pw", stop_event=stop)


class TestFetchCourses:
    def test_returns_clist(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/ajax/authCourse.aspx"] = FakeResponse(
            json_data={"clist": [{"cid": 11, "per": 60, "name": "课程A"}]})
        courses = welearn_common.fetch_courses(fake)
        assert courses == [{"cid": 11, "per": 60, "name": "课程A"}]

    def test_empty_clist_raises(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/ajax/authCourse.aspx"] = FakeResponse(
            json_data={"clist": []})
        with pytest.raises(WelearnError):
            welearn_common.fetch_courses(fake)


class TestGetCourseIds:
    def test_extracts_uid_and_classid(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/student/course_info.aspx"] = FakeResponse(
            text='var x = {"uid":12345,"name":"n"}; var y = {"classid":"C678"};')
        assert welearn_common.get_course_ids(fake, "99") == ("12345", "C678")


class TestFetchUnits:
    def test_returns_info_list(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx"] = FakeResponse(
            json_data={"info": [{"unitname": "U1", "name": "第一单元", "visible": "true"}]})
        units = welearn_common.fetch_units(fake, "1", "2")
        assert units[0]["unitname"] == "U1"
```

注意:`tests/fakes.py` 与测试文件同在 `tests/` 目录下,`from tests.fakes import ...` 需要仓库根目录存在 `tests/__init__.py`,本 Step 一并创建空文件 `tests/__init__.py`。

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_welearn_common_net.py -v`
Expected: FAIL,`AttributeError: module 'welearn_common' has no attribute 'login'`

- [ ] **Step 3: 实现网络函数(追加到 `welearn_common.py` 末尾,文件头补 `import threading`)**

```python
def _noop(msg):
    pass


def login(session, user: str, pwd: str,
          on_log: Callable[[str], None] = _noop,
          stop_event: Optional[threading.Event] = None) -> None:
    """在传入的 session 上完成登录;密码错误抛 LoginError,被停止抛 WelearnError。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            raise WelearnError("已停止")
        try:
            response = session.get(
                "https://welearn.sflep.com/user/prelogin.aspx?loginret=http://welearn.sflep.com/user/loginredirect.aspx")
            rurl = response.headers.get('Location')

            code_challenge = rurl.split("&")[4].split("=")[1]
            state = rurl.split("&")[6].split("=")[1]

            rturl = (f"/connect/authorize/callback?client_id=welearn_web"
                     f"&redirect_uri=https%3A%2F%2Fwelearn.sflep.com%2Fsignin-sflep"
                     f"&response_type=code&scope=openid%20profile%20email%20phone%20address"
                     f"&code_challenge={code_challenge}&code_challenge_method=S256"
                     f"&state={state}&x-client-SKU=ID_NET472&x-client-ver=6.32.1.0")
            on_log("登录中...")
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise WelearnError("已停止")
                enpwd = generate_cipher_text(pwd)

                form_data = {
                    "rturl": rturl,
                    "account": user,
                    "pwd": str(enpwd[0]),
                    "ts": str(enpwd[1])
                }

                response = session.post("https://sso.sflep.com/idsvr/account/login", data=form_data)

                rt_json = response.json()
                code = rt_json.get("code", -1)

                if code == -1:
                    continue

                if code == 1:
                    raise LoginError("帐号或密码错误!")

                next_url = "https://sso.sflep.com/idsvr" + rt_json.get("data")

                while True:
                    resp = session.get(next_url)
                    if resp.status_code in (301, 302, 303, 307, 308):
                        next_url = resp.headers.get("Location", None)
                        if not next_url:
                            break
                    else:
                        break

                if code == 0:
                    on_log("登录成功!")
                    return
        except (LoginError, WelearnError):
            raise
        except Exception as e:
            raise LoginError(f"错误返回,登录失败!错误信息:{e}")


def fetch_courses(session) -> List[dict]:
    response = session.get(
        'https://welearn.sflep.com/ajax/authCourse.aspx?action=gmc',
        headers={'Referer': 'https://welearn.sflep.com/2019/student/index.aspx'})
    clist = response.json().get('clist', [])
    if not clist:
        raise WelearnError('发生错误!!!可能是登录错误或没有课程!!!')
    return clist


def get_course_ids(session, cid: str) -> Tuple[str, str]:
    response = session.get(f"https://welearn.sflep.com/student/course_info.aspx?cid={cid}")
    uid = re.search('"uid":(.*?),', response.text).group(1)
    classid = re.search('"classid":"(.*?)"', response.text).group(1)
    return uid, classid


def fetch_units(session, cid: str, uid: str) -> List[dict]:
    response = session.get(
        'https://welearn.sflep.com/ajax/StudyStat.aspx',
        params={'action': 'courseunits', 'cid': cid, 'uid': uid},
        headers={'Referer': 'https://welearn.sflep.com/2019/student/course_info.aspx'})
    return response.json()['info']
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add welearn_common.py tests/fakes.py tests/__init__.py tests/test_welearn_common_net.py
git commit -m "feat: welearn_common 网络函数(登录/课程查询)及 FakeSession 测试"
```

---

### Task 3: 重构 `welearn_time.py` 为 `TimeTask` 控制器 + CLI 包装

**Files:**
- Modify: `welearn_time.py`(整文件重写,git 历史可追溯)
- Create: `tests/test_welearn_time.py`

**Interfaces:**
- Consumes: Task 2 的 `login`/`fetch_courses`/`get_course_ids`/`fetch_units`/`parse_numeric_param`
- Produces(Task 5 GUI 按此引用):
  - `class TimeTask(username: str, password: str, on_log: Callable[[str], None] = None, stop_event: threading.Event = None)`;属性 `request_interval: int = 2`(测试可改 0)
  - `login() -> None`;`fetch_courses() -> List[dict]`;`fetch_units(cid) -> List[dict]`(内部记住 `self.cid/uid/classid/units`)
  - `run(unit_index: int, param) -> Dict[str, Any]`,param 为 `int` 或 `(lo, hi)`;返回 `{"total": int, "succeed": int, "failed": int, "errors": List[str]}`
  - 模块级 `welearn_time_run()`(CLI 包装,原行为)
- 说明:原 `simulate` 的多个 `print`(章节/已学、方式结果)合并为按行 `on_log`;心跳由原 `\r` 每秒刷新改为每 10 秒输出一行(GUI 的 Text 控件不支持 `\r`,且避免刷屏)

- [ ] **Step 1: 写失败测试**

`tests/test_welearn_time.py`:

```python
import json
import threading
import time

from tests.fakes import FakeResponse, FakeSession
import welearn_time


def make_task(fake, chapters):
    task = welearn_time.TimeTask("u", "p", on_log=lambda m: None)
    task.session = fake
    task.cid, task.uid, self_classid = "C1", "U1", "K1"
    task.classid = self_classid
    task.units = [{"unitname": "U1", "name": "Unit One"}]
    task.request_interval = 0
    fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
        FakeResponse(json_data={"info": chapters})
    return task


def ok_ajax_response():
    cmi = {"score": {"scaled": ""}, "completion_status": "unknown",
           "progress_measure": "0", "total_time": "0", "session_time": "0"}
    return FakeResponse(json_data={"comment": json.dumps({"cmi": cmi})})


class TestRun:
    def test_counts_chapters(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = ok_ajax_response()
        chapters = [{"id": 1, "location": "A", "learntime": 0},
                    {"id": 2, "location": "B", "learntime": 0}]
        task = make_task(fake, chapters)
        result = task.run(0, 2)
        assert result["total"] == 2
        assert result["failed"] == 0
        assert result["succeed"] == 2

    def test_bad_sco_recorded_as_error(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = \
            FakeResponse(text="学习数据不正确")
        chapters = [{"id": 1, "location": "BadChapter", "learntime": 0}]
        task = make_task(fake, chapters)
        result = task.run(1, 1)
        assert result["failed"] == 1
        assert result["errors"] == ["BadChapter"]

    def test_stop_interrupts_quickly(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = ok_ajax_response()
        chapters = [{"id": 1, "location": "A", "learntime": 0},
                    {"id": 2, "location": "B", "learntime": 0}]
        task = make_task(fake, chapters)
        threading.Timer(1.0, task.stop_event.set).start()
        start = time.time()
        result = task.run(0, 60)
        elapsed = time.time() - start
        assert elapsed < 10
        assert task.stop_event.is_set()
        assert isinstance(result["total"], int)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_welearn_time.py -v`
Expected: FAIL,`AttributeError: module 'welearn_time' has no attribute 'TimeTask'`

- [ ] **Step 3: 重写 `welearn_time.py`**

```python
import asyncio
import json
import random
import threading
import time
from textwrap import dedent
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import welearn_common
from welearn_common import LoginError, WelearnError

REQUEST_INTERVAL = 2
HEARTBEAT_INTERVAL = 1
AJAX_URL = "https://welearn.sflep.com/Ajax/SCO.aspx"


def _default_log(msg):
    print(msg)


class TimeTask:
    """刷学习时长任务控制器(CLI 与 GUI 共用)。所有输出经 on_log 回调,可在工作线程中调用。"""

    def __init__(self, username: str, password: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None):
        self.username = username
        self.password = password
        self.on_log = on_log or _default_log
        self.stop_event = stop_event or threading.Event()
        self.session = welearn_common.new_session()
        self.cid = ''
        self.uid = ''
        self.classid = ''
        self.units: List[Any] = []
        self.request_interval = REQUEST_INTERVAL

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def login(self) -> None:
        welearn_common.login(self.session, self.username, self.password,
                             on_log=self.on_log, stop_event=self.stop_event)

    def fetch_courses(self) -> List[dict]:
        return welearn_common.fetch_courses(self.session)

    def fetch_units(self, cid) -> List[dict]:
        self.cid = str(cid)
        self.uid, self.classid = welearn_common.get_course_ids(self.session, self.cid)
        self.units = welearn_common.fetch_units(self.session, self.cid, self.uid)
        return self.units

    def _generate_learning_time(self, target_time) -> int:
        if type(target_time) is int:
            return target_time
        return random.randint(target_time[0], target_time[1])

    def run(self, unit_index: int, param) -> Dict[str, Any]:
        target_time = param
        errors: List[str] = []
        max_learning_time = 0

        if unit_index == 0:
            start_index, end_index = 0, len(self.units)
        else:
            start_index, end_index = unit_index - 1, unit_index

        pending: List[Tuple[int, dict]] = []
        for unit in range(start_index, end_index):
            if self._stopped():
                break
            response = self.session.get(
                f"https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={self.cid}&uid={self.uid}&unitidx={str(unit)}&classid={self.classid}",
                headers={'Referer': 'https://welearn.sflep.com/2019/student/course_info.aspx?cid=' + self.cid})
            for chapter in response.json()['info']:
                learning_time = self._generate_learning_time(target_time)
                if learning_time > max_learning_time:
                    max_learning_time = learning_time
                pending.append((learning_time, chapter))

        async def runner():
            coros = [self._simulate(learning_time, chapter, errors)
                     for learning_time, chapter in pending]
            await asyncio.gather(self._heartbeat(max_learning_time), *coros)

        asyncio.run(runner())
        return {'total': len(pending), 'succeed': len(pending) - len(errors),
                'failed': len(errors), 'errors': list(errors)}

    async def _simulate(self, learning_time: int, chapter: Dict, errors: List[str]) -> None:
        self.on_log(f"章节 : {chapter['location']}")
        self.on_log(f"已学 : {chapter['learntime']} 将学 : {learning_time}")

        common_headers = {'Referer': 'https://welearn.sflep.com/student/StudyCourse.aspx'}
        common_data = {'uid': self.uid, 'cid': self.cid, 'scoid': chapter['id']}

        await asyncio.sleep(self.request_interval)
        response = self.session.post(
            AJAX_URL, data={**common_data, 'action': 'getscoinfo_v7'}, headers=common_headers)

        if '学习数据不正确' in response.text:  # 重试
            await asyncio.sleep(self.request_interval)
            self.session.post(
                AJAX_URL, data={**common_data, 'action': 'startsco160928'}, headers=common_headers)
            response = self.session.post(
                AJAX_URL, data={**common_data, 'action': 'getscoinfo_v7'}, headers=common_headers)
            if '学习数据不正确' in response.text:
                self.on_log(f"错误: {chapter['location']}")
                errors.append(chapter['location'])
                return

        return_json = response.json()['comment']
        if 'cmi' in return_json:
            cmi = json.loads(return_json)['cmi']
            crate = cmi['score']['scaled']
            cstatus = cmi['completion_status']
            progress = cmi['progress_measure']
            total_time = cmi['total_time']
            session_time = cmi['session_time']
        else:
            crate = ''
            cstatus = 'not_attempted'
            progress = '0'
            total_time = '0'
            session_time = '0'

        await asyncio.sleep(self.request_interval)
        self.session.post(
            AJAX_URL,
            data={**common_data, 'action': 'keepsco_with_getticket_with_updatecmitime',
                  'session_time': session_time, 'total_time': total_time},
            headers=common_headers)

        for current_time in range(1, learning_time + 1):
            if self._stopped():
                self.on_log(f"任务已停止,跳过剩余时长: {chapter['location']}")
                return
            await asyncio.sleep(1)
            if current_time % 60 == 0:
                self.session.post(
                    AJAX_URL,
                    data={**common_data, 'action': 'keepsco_with_getticket_with_updatecmitime',
                          'session_time': session_time, 'total_time': total_time},
                    headers=common_headers)

        await asyncio.sleep(self.request_interval)
        self.session.post(
            AJAX_URL,
            data={**common_data, 'action': 'savescoinfo160928',
                  'crate': crate, 'cstatus': cstatus, 'status': 'unknown',
                  'progress': progress, 'trycount': '0'},
            headers=common_headers)

    async def _heartbeat(self, max_learning_time: int) -> None:
        start_time = time.time()
        estimate = max_learning_time + 4 * self.request_interval
        next_report = 10
        for _ in range(estimate):
            if self._stopped():
                return
            if int(time.time() - start_time) >= next_report:
                self.on_log(f"预计学习时长 : {estimate} 已学习时长 : {int(time.time() - start_time)}")
                next_report += 10
            await asyncio.sleep(HEARTBEAT_INTERVAL)


def welearn_time_run():
    user = input("请输入用户名=>")
    pwd = input("请输入密码=>")
    task = TimeTask(user, pwd)
    try:
        task.login()
    except (LoginError, WelearnError) as e:
        print(e)
        exit(0)
    while True:
        try:
            courses = task.fetch_courses()
        except WelearnError as e:
            input(str(e))
            exit(0)
        for index, course in enumerate(courses, start=1):
            print('[id:{:>2d}]  完成度 {:>3d}%  {}'.format(index, course['per'], course['name']))

        index = int(input('\n请输入需要刷时长的课程id(id为上方[]内的序号): '))
        units = task.fetch_units(courses[index - 1]['cid'])

        print("\n\n")
        print('[id: 0]  按顺序刷全部单元学习时长')
        for index, unit in enumerate(units, start=1):
            print(f"[id:{index:>2d}]  {unit['unitname']}  {unit['name']}")
        print("\n\n")
        unit_index = int(input('请选择要刷时长的单元id(id为上方[]内的序号,输入0为刷全部单元): '))

        print("\n\n")
        print(dedent('''\
            模式1:每个练习增加指定学习时长,请直接输入时间
            如:希望每个练习增加30秒,则输入 30

            模式2:每个练习增加随机时长,请输入时间上下限并用英文逗号隔开
            如:希望每个练习增加10~30秒,则输入 10,30
        '''))
        print("\n\n")

        input_ = input("请严格按照以上格式输入: ")
        try:
            target_time = welearn_common.parse_numeric_param(input_)
        except ValueError:
            print("格式异常")
            exit(0)

        result = task.run(unit_index, target_time)

        print('运行结束!!\n错误:', result['failed'], '个')
        for index, error in enumerate(result['errors'], start=1):
            print(f'第{index}个错误:{error}')

        if unit_index == 0:
            break
        print("\n\n")
        print('回到选课处!!')
        print("\n\n")

    print("\n\n")
    print(dedent('''\
        **********  Created By Avenshy & SSmJaE  **********
                        Version : 0.3.0
           基于GPL3.0,完全开源,免费,禁止二次倒卖或商用
           https://www.github.com/Avenshy/WELearnToSleeep
                        仅供学习,勿滥用
        ***************************************************
    '''))
    input("Press any key to exit...")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS(`test_stop_interrupts_quickly` 约 1~3 秒完成)

- [ ] **Step 5: CLI 冒烟**

Run: `python -c "import welearn_time; print(hasattr(welearn_time, 'TimeTask'), hasattr(welearn_time, 'welearn_time_run'))"`
Expected: `True True`

- [ ] **Step 6: Commit**

```bash
git add welearn_time.py tests/test_welearn_time.py
git commit -m "refactor: welearn_time 重构为 TimeTask 控制器(支持 on_log 回调与停止事件),保留 CLI 入口"
```

---

### Task 4: 重构 `welearn_accuracy.py` 为 `AccuracyTask` 控制器 + CLI 包装

**Files:**
- Modify: `welearn_accuracy.py`(整文件重写)
- Create: `tests/test_welearn_accuracy.py`

**Interfaces:**
- Consumes: Task 2 的共享函数;Task 1 的 `parse_numeric_param`(CLI 侧)
- Produces(Task 5 GUI 按此引用):
  - `class AccuracyTask(username: str, password: str, on_log: Callable[[str], None] = None, stop_event: threading.Event = None)`
  - `login()` / `fetch_courses()` / `fetch_units(cid)`(同 `TimeTask`)
  - `run(unit_index: int, param) -> Dict[str, Any]`,param 为正确率 `int` 或 `(lo, hi)`;返回 `{"way1_succeed": int, "way1_failed": int, "way2_succeed": int, "way2_failed": int}`
  - 模块级 `welearn_accuracy_run()`(CLI 包装,原行为;原 `eval` 换成 `parse_numeric_param`)

- [ ] **Step 1: 写失败测试**

`tests/test_welearn_accuracy.py`:

```python
from tests.fakes import FakeResponse, FakeSession
import welearn_accuracy


def make_task(fake, chapters):
    task = welearn_accuracy.AccuracyTask("u", "p", on_log=lambda m: None)
    task.session = fake
    task.cid, task.uid, task.classid = "C1", "U1", "K1"
    task.units = [{"unitname": "U1", "name": "Unit One", "visible": "true"}]
    fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
        FakeResponse(json_data={"info": chapters})
    return task


class TestRun:
    def test_counters_and_posts(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":0}')
        chapters = [
            {"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"},
            {"id": 2, "location": "L2", "isvisible": "false", "iscomplete": "completed"},
            {"id": 3, "location": "L3", "isvisible": "true", "iscomplete": "completed"},
        ]
        task = make_task(fake, chapters)
        result = task.run(0, 100)
        assert result["way1_succeed"] == 1
        assert result["way2_succeed"] == 1
        assert result["way1_failed"] == 0
        assert result["way2_failed"] == 0
        sco_posts = [r for r in fake.requests if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"]
        assert len(sco_posts) == 3   # 仅未完成章节: startsco + setsco + savesco

    def test_failure_counters(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":1}')
        chapters = [{"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"}]
        task = make_task(fake, chapters)
        result = task.run(1, (70, 100))
        assert result["way1_failed"] == 1
        assert result["way2_failed"] == 1
        data = fake.requests[1][2]   # setscoinfo 的 data
        crate = data["data"].split('"scaled":"')[1].split('"')[0]
        assert 70 <= int(crate) <= 100

    def test_stops_mid_unit(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":0}')
        chapters = [{"id": i, "location": f"L{i}", "isvisible": "true", "iscomplete": "未完成"}
                    for i in range(1, 6)]
        task = make_task(fake, chapters)
        task.stop_event.set()
        result = task.run(0, 100)
        sco_posts = [r for r in fake.requests if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"]
        assert len(sco_posts) == 0   # 循环入口即检查停止
        assert result["way1_succeed"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_welearn_accuracy.py -v`
Expected: FAIL,`AttributeError: module 'welearn_accuracy' has no attribute 'AccuracyTask'`

- [ ] **Step 3: 重写 `welearn_accuracy.py`**

```python
import threading
from random import randint
from typing import Any, Callable, Dict, List, Optional

import welearn_common
from welearn_common import LoginError, WelearnError

AJAX_URL = "https://welearn.sflep.com/Ajax/SCO.aspx"

# 伪造的完成数据报文(与原实现逐字一致)
SCO_DATA_TEMPLATE = ('{"cmi":{"completion_status":"completed","interactions":[],"launch_data":"",'
                     '"progress_measure":"1","score":{"scaled":"{crate}","raw":"100"},'
                     '"session_time":"0","success_status":"unknown","total_time":"0","mode":"normal"},'
                     '"adl":{"data":[]},"cci":{"data":[],"service":{"dictionary":{"headword":"",'
                     '"short_cuts":""},"new_words":[],"notes":[],"writing_marking":[],"record":'
                     '{"files":[]},"play":{"offline_media_id":"9999"}},"retry_count":"0",'
                     '"submit_time":""}}[INTERACTIONINFO]')


def _default_log(msg):
    print(msg)


class AccuracyTask:
    """刷完成度正确率任务控制器(CLI 与 GUI 共用)。所有输出经 on_log 回调,可在工作线程中调用。"""

    def __init__(self, username: str, password: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None):
        self.username = username
        self.password = password
        self.on_log = on_log or _default_log
        self.stop_event = stop_event or threading.Event()
        self.session = welearn_common.new_session()
        self.cid = ''
        self.uid = ''
        self.classid = ''
        self.units: List[Any] = []

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def login(self) -> None:
        welearn_common.login(self.session, self.username, self.password,
                             on_log=self.on_log, stop_event=self.stop_event)

    def fetch_courses(self) -> List[dict]:
        return welearn_common.fetch_courses(self.session)

    def fetch_units(self, cid) -> List[dict]:
        self.cid = str(cid)
        self.uid, self.classid = welearn_common.get_course_ids(self.session, self.cid)
        self.units = welearn_common.fetch_units(self.session, self.cid, self.uid)
        return self.units

    def run(self, unit_index: int, param) -> Dict[str, Any]:
        random_mode = isinstance(param, tuple)
        way1_succeed, way2_succeed = 0, 0
        way1_failed, way2_failed = 0, 0

        info_headers = {
            "Referer": f"https://welearn.sflep.com/2019/student/course_info.aspx?cid={self.cid}",
        }

        if unit_index == 0:
            start, end = 0, len(self.units)
        else:
            start, end = unit_index - 1, unit_index

        for unit in range(start, end):
            if self._stopped():
                break
            response = self.session.get(
                f'https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={self.cid}&uid={self.uid}&unitidx={unit}&classid={self.classid}',
                headers=info_headers)

            if "异常" in response.text or "出错了" in response.text:
                break

            for course in response.json()["info"]:
                if self._stopped():
                    break
                if course['isvisible'] == 'false':  # 跳过未开放课程
                    self.on_log(f'[!!跳过!!]    {course["location"]}')
                elif "未" in course["iscomplete"]:  # 章节未完成
                    self.on_log(f'[即将完成]    {course["location"]}')
                    if random_mode:
                        crate = str(randint(param[0], param[1]))
                    else:
                        crate = str(param)
                    data = SCO_DATA_TEMPLATE.replace('{crate}', crate)
                    sco_headers = {
                        "Referer": f"https://welearn.sflep.com/Student/StudyCourse.aspx?cid={self.cid}&classid={self.classid}&sco={course['id']}"}

                    self.session.post(AJAX_URL, data={"action": "startsco160928",
                                                      "cid": self.cid,
                                                      "scoid": course["id"],
                                                      "uid": self.uid},
                                      headers=sco_headers)
                    response = self.session.post(AJAX_URL, data={"action": "setscoinfo",
                                                                 "cid": self.cid,
                                                                 "scoid": course["id"],
                                                                 "uid": self.uid,
                                                                 "data": data,
                                                                 "isend": "False"},
                                                 headers=sco_headers)
                    way1_ok = '"ret":0' in response.text
                    if way1_ok:
                        way1_succeed += 1
                    else:
                        way1_failed += 1

                    response = self.session.post(AJAX_URL, data={"action": "savescoinfo160928",
                                                                 "cid": self.cid,
                                                                 "scoid": course["id"],
                                                                 "uid": self.uid,
                                                                 "progress": "100",
                                                                 "crate": crate,
                                                                 "status": "unknown",
                                                                 "cstatus": "completed",
                                                                 "trycount": "0",
                                                                 },
                                                 headers=sco_headers)
                    way2_ok = '"ret":0' in response.text
                    if way2_ok:
                        way2_succeed += 1
                    else:
                        way2_failed += 1
                    self.on_log(f'>>>>>>>>>>>>>>正确率:{crate:>3}%  '
                                f'方式1:{"成功!!!" if way1_ok else "失败!!!"}  '
                                f'方式2:{"成功!!!" if way2_ok else "失败!!!"}')
                else:  # 章节已完成
                    self.on_log(f'[ 已完成 ]    {course["location"]}')

        return {'way1_succeed': way1_succeed, 'way1_failed': way1_failed,
                'way2_succeed': way2_succeed, 'way2_failed': way2_failed}


def _printline():
    print('-' * 51)


def welearn_accuracy_run():
    user = input("请输入用户名=>")
    pwd = input("请输入密码=>")
    task = AccuracyTask(user, pwd)
    try:
        task.login()
    except (LoginError, WelearnError) as e:
        print(e)
        exit(0)
    _printline()
    while True:
        try:
            courses = task.fetch_courses()
        except WelearnError as e:
            input(str(e))
            exit(0)
        print('查询课程成功!!!')
        _printline()
        print('我的课程: \n')

        for i, course in enumerate(courses, start=1):
            print(f'[NO.{i:>2}] 完成度{course["per"]:>3}% {course["name"]}')

        order = int(input("\n请输入需要完成的课程序号(上方[]内的数字): "))
        cid = courses[order - 1]["cid"]
        _printline()
        print("获取单元中...")
        _printline()

        units = task.fetch_units(cid)

        print('[NO. 0]  按顺序完成全部单元课程')
        for i, x in enumerate(units, start=1):
            if x['visible'] == 'true':
                print(f'[NO.{i:>2d}]  [已开放]  {x["unitname"]}  {x["name"]}')
            else:
                print(f'[NO.{i:>2d}] ![未开放]! {x["unitname"]}  {x["name"]}')
        unitidx = int(input('\n\n请选择需要完成的单元序号(上方[]内的数字,输入0为按顺序刷全部单元): '))
        _printline()
        inputcrate = input(
            '模式1:每个练习指定正确率,请直接输入指定的正确率\n如:希望每个练习正确率均为100,则输入 100\n\n模式2:每个练习随机正确率,请输入正确率上下限并用英文逗号隔开\n如:希望每个练习正确率为70~100,则输入 70,100\n\n请严格按照以上格式输入每个练习的正确率: ')
        try:
            mycrate = welearn_common.parse_numeric_param(inputcrate)
        except ValueError:
            print("格式异常")
            exit(0)
        _printline()

        result = task.run(unitidx, mycrate)

        if unitidx == 0:
            break
        print('本单元运行完毕!回到选课处!!\n\n\n\n')
        _printline()

    _printline()
    print(f"""
    ***************************************************
    全部完成!!

    总计:
    方式1: {result['way1_succeed']} 成功, {result['way1_failed']} 失败
    方式2: {result['way2_succeed']} 成功, {result['way2_failed']} 失败

    https://github.com/Avenshy/WELearnToSleep
    本软件遵守GPLv3协议,且免费、开源,禁止售卖!
    本软件遵守GPLv3协议,且免费、开源,禁止售卖!
    本软件遵守GPLv3协议,且免费、开源,禁止售卖!
    **********  Created By Avenshy & SSmJaE  **********""")
    input("Press any key to exit...")
```

注意:伪造报文模板把原实现的字符串拼接改为 `{crate}` 占位 + `str.replace`,生成结果与原字符串逐字节一致(测试 `test_failure_counters` 校验了 `"scaled":"70~100"` 字段确实落位)。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add welearn_accuracy.py tests/test_welearn_accuracy.py
git commit -m "refactor: welearn_accuracy 重构为 AccuracyTask 控制器(eval 改安全解析),保留 CLI 入口"
```

---

### Task 5: `gui.py` 图形界面(tkinter + 后台线程)

**Files:**
- Create: `gui.py`

**Interfaces:**
- Consumes: Task 3 `TimeTask`、Task 4 `AccuracyTask`、Task 1 `parse_numeric_param`、Task 2 异常类
- Produces: `python gui.py` 可启动的完整界面;`WelearnApp` 类(无后续任务依赖)

线程安全铁律(实现时必须遵守):
- 工作线程里**绝不**直接碰控件;所有 UI 更新经 `self.root.after(0, ...)` 调度回主线程
- `on_log` 回调只做 `queue.Queue.put`;主线程 `after(100ms)` 轮询取出写日志

- [ ] **Step 1: 创建 `gui.py`**

```python
"""Welearn_helper 图形界面(tkinter)。刷课任务在后台线程运行,可随时停止。"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import List

import welearn_accuracy
import welearn_common
import welearn_time
from welearn_common import LoginError, WelearnError

BANNER = """**********  Created By Avenshy & SSmJaE  **********
                 图形界面Version:1.0
        原项目作者github https://github.com/Avenshy
        更新维护者github https://github.com/lxbsdsg/Welearn_helper/
***************************************************"""


class WelearnApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Welearn_helper 图形界面")
        root.geometry("980x680")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread = None
        self.task = None            # AccuracyTask 或 TimeTask
        self.courses: List[dict] = []
        self.units: List[dict] = []

        self._build_widgets()
        self._set_running(False)
        self.root.after(100, self._poll_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 界面搭建 ----------
    def _build_widgets(self):
        ttk.Label(self.root, text=BANNER, font=("Consolas", 10), justify=tk.CENTER)\
            .pack(pady=(8, 4))

        login_frame = ttk.LabelFrame(self.root, text="登录")
        login_frame.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(login_frame, text="账号:").grid(row=0, column=0, padx=4, pady=4, sticky=tk.W)
        self.username_entry = ttk.Entry(login_frame, width=16)
        self.username_entry.grid(row=0, column=1, padx=4)
        ttk.Label(login_frame, text="密码:").grid(row=0, column=2, padx=4)
        self.password_entry = ttk.Entry(login_frame, width=16, show="*")
        self.password_entry.grid(row=0, column=3, padx=4)
        self.mode_var = tk.StringVar(value="accuracy")
        ttk.Radiobutton(login_frame, text="刷完成度正确率", variable=self.mode_var,
                        value="accuracy", command=self._on_mode_changed)\
            .grid(row=0, column=4, padx=(12, 2))
        ttk.Radiobutton(login_frame, text="刷学习时长", variable=self.mode_var,
                        value="time", command=self._on_mode_changed)\
            .grid(row=0, column=5, padx=2)
        self.login_button = ttk.Button(login_frame, text="登录", command=self._on_login)
        self.login_button.grid(row=0, column=6, padx=12)

        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        course_frame = ttk.LabelFrame(list_frame, text="课程(登录后自动加载,选中后加载单元)")
        course_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.course_tree = ttk.Treeview(course_frame, columns=("per", "name"),
                                        show="headings", height=8)
        self.course_tree.heading("per", text="完成度%")
        self.course_tree.heading("name", text="课程名称")
        self.course_tree.column("per", width=70, anchor=tk.E)
        self.course_tree.column("name", width=280)
        self.course_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.course_tree.bind("<<TreeviewSelect>>", self._on_course_selected)
        self.refresh_button = ttk.Button(course_frame, text="刷新课程", command=self._on_refresh)
        self.refresh_button.pack(pady=(0, 4))

        unit_frame = ttk.LabelFrame(list_frame, text="单元(选中课程后加载,选中行自动填入单元id)")
        unit_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.unit_tree = ttk.Treeview(unit_frame, columns=("id", "unitname", "name", "visible"),
                                      show="headings", height=8)
        self.unit_tree.heading("id", text="id")
        self.unit_tree.heading("unitname", text="单元")
        self.unit_tree.heading("name", text="名称")
        self.unit_tree.heading("visible", text="开放")
        self.unit_tree.column("id", width=40, anchor=tk.E)
        self.unit_tree.column("unitname", width=120)
        self.unit_tree.column("name", width=180)
        self.unit_tree.column("visible", width=60, anchor=tk.CENTER)
        self.unit_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.unit_tree.bind("<<TreeviewSelect>>", self._on_unit_selected)

        param_frame = ttk.LabelFrame(self.root, text="任务参数")
        param_frame.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(param_frame, text="单元id(0=全部):").pack(side=tk.LEFT, padx=4)
        self.unit_entry = ttk.Entry(param_frame, width=6)
        self.unit_entry.pack(side=tk.LEFT, padx=4)
        self.unit_entry.insert(0, "0")
        self.param_label = ttk.Label(param_frame, text="正确率(100 或 70,100):")
        self.param_label.pack(side=tk.LEFT, padx=(16, 4))
        self.param_entry = ttk.Entry(param_frame, width=12)
        self.param_entry.pack(side=tk.LEFT, padx=4)

        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill=tk.X, padx=8, pady=4)
        self.start_button = ttk.Button(control_frame, text="开始", command=self._on_start)
        self.start_button.pack(side=tk.LEFT, padx=4)
        self.stop_button = ttk.Button(control_frame, text="停止", command=self._on_stop,
                                      state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=4)
        self.status_var = tk.StringVar(value="待命")
        ttk.Label(control_frame, textvariable=self.status_var).pack(side=tk.RIGHT, padx=8)

        log_frame = ttk.LabelFrame(self.root, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, state=tk.DISABLED,
                                                  font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ---------- 日志(线程安全:只入队,主线程轮询写出) ----------
    def _worker_log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.root.after(100, self._poll_log_queue)

    def _log_gui(self, msg: str):
        self.log_queue.put(msg)   # 主线程也走队列,保证顺序一致

    # ---------- 状态 ----------
    def _set_running(self, running: bool, running_what: str = None):
        off = tk.DISABLED if running else tk.NORMAL
        self.login_button.configure(state=off)
        self.start_button.configure(state=off)
        self.refresh_button.configure(state=off)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        for entry in (self.username_entry, self.password_entry,
                      self.unit_entry, self.param_entry):
            entry.configure(state='readonly' if running else 'normal')
        for radio in self.root.winfo_children():
            pass   # 单选按钮保持可用(切换模式会要求重新登录,无碍)
        self.status_var.set(running_what if running_what else "待命")

    def _on_mode_changed(self):
        self.task = None
        self.courses = []
        self.units = []
        for tree in (self.course_tree, self.unit_tree):
            tree.delete(*tree.get_children())
        if self.mode_var.get() == "accuracy":
            self.param_label.configure(text="正确率(100 或 70,100):")
        else:
            self.param_label.configure(text="每练习时长秒(30 或 10,30):")
        self._log_gui("已切换模式,请重新登录")

    # ---------- 登录 ----------
    def _on_login(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            messagebox.showwarning("提示", "请输入账号和密码")
            return
        self._set_running(True, "登录中...")
        mode = self.mode_var.get()

        def work():
            cls = welearn_accuracy.AccuracyTask if mode == "accuracy" else welearn_time.TimeTask
            task = cls(username, password, on_log=self._worker_log,
                       stop_event=self.stop_event)
            try:
                task.login()
                courses = task.fetch_courses()
                self.root.after(0, lambda: self._after_login(True, task, courses, None))
            except (LoginError, WelearnError) as e:
                self.root.after(0, lambda: self._after_login(False, None, None, str(e)))
            except Exception as e:
                self.root.after(0, lambda: self._after_login(False, None, None, repr(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _after_login(self, ok, task, courses, error):
        self._set_running(False)
        if not ok:
            messagebox.showerror("登录失败", error)
            return
        self.task = task
        self.courses = courses
        self._fill_courses()
        self._log_gui(f"共 {len(courses)} 门课程,请在左侧选择课程")

    def _fill_courses(self):
        self.course_tree.delete(*self.course_tree.get_children())
        for i, course in enumerate(self.courses, start=1):
            self.course_tree.insert("", tk.END, iid=str(i),
                                    values=(course.get("per", 0), course.get("name", "")))

    def _on_refresh(self):
        if self.task is None:
            messagebox.showwarning("提示", "请先登录")
            return
        self._set_running(True, "刷新课程中...")

        def work():
            try:
                courses = self.task.fetch_courses()
                self.root.after(0, lambda: self._after_refresh(courses, None))
            except (LoginError, WelearnError) as e:
                self.root.after(0, lambda: self._after_refresh(None, str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _after_refresh(self, courses, error):
        self._set_running(False)
        if error is not None:
            messagebox.showerror("刷新失败", error)
            return
        self.courses = courses
        self._fill_courses()
        self._log_gui(f"已刷新,共 {len(courses)} 门课程")

    # ---------- 课程 → 单元 ----------
    def _selected_course(self):
        selection = self.course_tree.selection()
        if not selection:
            return None
        return self.courses[int(selection[0]) - 1]

    def _on_course_selected(self, _event):
        course = self._selected_course()
        if course is None or self.task is None:
            return
        if self.worker is not None and self.worker.is_alive():
            return
        cid = course["cid"]
        self._set_running(True, "加载单元中...")

        def work():
            try:
                units = self.task.fetch_units(cid)
                self.root.after(0, lambda: self._after_units(units, None))
            except (LoginError, WelearnError) as e:
                self.root.after(0, lambda: self._after_units(None, str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _after_units(self, units, error):
        self._set_running(False)
        if error is not None:
            messagebox.showerror("加载单元失败", error)
            return
        self.units = units
        self.unit_tree.delete(*self.unit_tree.get_children())
        self.unit_tree.insert("", tk.END, iid="0", values=(0, "全部单元", "按顺序刷全部", ""))
        for i, unit in enumerate(self.units, start=1):
            visible = "是" if str(unit.get("visible", "true")).lower() == "true" else "否"
            self.unit_tree.insert("", tk.END, iid=str(i),
                                  values=(i, unit.get("unitname", ""), unit.get("name", ""),
                                          visible))
        self._log_gui(f"已加载 {len(self.units)} 个单元,选中行自动填入单元id")

    def _on_unit_selected(self, _event):
        selection = self.unit_tree.selection()
        if selection:
            self.unit_entry.delete(0, tk.END)
            self.unit_entry.insert(0, selection[0])

    # ---------- 任务 ----------
    def _on_start(self):
        if self.task is None:
            messagebox.showwarning("提示", "请先登录")
            return
        if not self.units:
            messagebox.showwarning("提示", "请先选择课程加载单元")
            return
        try:
            unit_index = int(self.unit_entry.get())
        except ValueError:
            messagebox.showwarning("提示", "单元id必须是数字(0=全部)")
            return
        try:
            param = welearn_common.parse_numeric_param(self.param_entry.get())
        except ValueError as e:
            messagebox.showwarning("提示", f"参数格式错误:{e}\n模式1: 100   模式2: 70,100")
            return
        self.stop_event.clear()
        self._set_running(True, "任务运行中")
        self._log_gui(f"开始任务:单元id={unit_index} 参数={param}")

        def work():
            try:
                result = self.task.run(unit_index, param)
                self.root.after(0, lambda: self._after_run(True, result, None))
            except Exception as e:
                self.root.after(0, lambda: self._after_run(False, None, repr(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _after_run(self, ok, result, error):
        self._set_running(False)
        if not ok:
            self._log_gui(f"任务异常终止:{error}")
            return
        if self.mode_var.get() == "time":
            self._log_gui(f"运行结束!!错误: {result['failed']} 个")
            for i, err in enumerate(result["errors"], start=1):
                self._log_gui(f"第{i}个错误:{err}")
        else:
            self._log_gui("方式1: {} 成功, {} 失败".format(result["way1_succeed"],
                                                         result["way1_failed"]))
            self._log_gui("方式2: {} 成功, {} 失败".format(result["way2_succeed"],
                                                         result["way2_failed"]))

    def _on_stop(self):
        self.stop_event.set()
        self._log_gui("已请求停止,等待任务收尾...")

    def _on_close(self):
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askokcancel("退出", "任务正在运行,确定停止并退出吗?"):
                return
            self.stop_event.set()
            self.worker.join(timeout=5)
        self.root.destroy()


def main():
    root = tk.Tk()
    WelearnApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: 语法检查**

Run: `python -m py_compile gui.py && echo OK`
Expected: `OK`

- [ ] **Step 3: 冒烟启动**

Run: `python gui.py`(人工关闭窗口结束)
Expected: 窗口打开;横幅显示 Avenshy & SSmJaE 与 lxbsdsg 更新维护者;各控件齐全;开始/停止按钮初始为 启用/禁用;关闭窗口无报错

- [ ] **Step 4: 运行全量测试确认无回归**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add gui.py
git commit -m "feat: 新增 tkinter 图形界面(后台线程、实时日志、可停止)"
```

---

### Task 6: `main.py` 横幅、README、启动脚本(作者信息 + 双入口)

**Files:**
- Modify: `main.py:7`
- Modify: `README.md`
- Modify: `一键启动.bat`
- Create: `命令行启动.bat`

**Interfaces:**
- Consumes: 无
- Produces: 双入口启动脚本;作者信息更新(验收标准的一部分)

- [ ] **Step 1: 修改 `main.py` 横幅**

第 7 行:

```python
print("         更新维护者github https://github.com/lxbsdsg/Welearn_helper/")
```

(原为 `https://github.com/YZBRH/Welearn_helper/`,其余行不动)

- [ ] **Step 2: 更新 `README.md`**

声明部分改为:

```markdown
# 声明

本项目原作者为https://github.com/Avenshy

原项目已停止更新维护,无法直接使用,现进行修正调整后已恢复其原功能

该脚本版权归于原作者,本项目仅进行修复优化

现由 [lxbsdsg](https://github.com/lxbsdsg/Welearn_helper) 更新维护

# 使用

图形界面(推荐):双击 一键启动.bat,或手动运行 gui.py

命令行:双击 命令行启动.bat,或手动运行 main.py,按照提示进行操作

# 更新日志

2024.12.18

增加了main.py聚合 并且在 dist/ 有可执行文件可供使用



2025.6.3

更新登录逻辑,修复了无法正常登录的问题



2026.10.13

新增图形界面(多线程不卡顿、可随时停止),保留命令行入口;更新维护者变更为 lxbsdsg
```

- [ ] **Step 3: 更新 `一键启动.bat`、新建 `命令行启动.bat`**

`一键启动.bat`(整体替换):

```bat
python -u gui.py
pause
```

`命令行启动.bat`(新建):

```bat
python -u main.py
pause
```

注意:两个 .bat 内容保持纯 ASCII;文件本身以原有编码方式保存(沿用现有 `一键启动.bat` 的格式)。

- [ ] **Step 4: 验证**

Run: `python main.py < nul`(立即收到 EOF 退出即可,横幅已打印)或直接 `python main.py` 后按 0
Expected: 横幅第 7 行为 `更新维护者github https://github.com/lxbsdsg/Welearn_helper/`
Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add main.py README.md 一键启动.bat 命令行启动.bat
git commit -m "feat: 更新维护者改为 lxbsdsg;一键启动改为 GUI,新增命令行启动脚本"
```

---

### Task 7: 端到端手工验收

**Files:**
- 无代码改动(发现问题则回到对应任务修复后重跑)

**Interfaces:**
- Consumes: 全部前序任务成果
- Produces: 验收记录(贴回对话)

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 2: GUI 验收清单**

逐项执行并记录:

1. `python gui.py` 启动:横幅作者信息正确(原项目作者 Avenshy、更新维护者 lxbsdsg)
2. 空账号点登录 → 警告弹窗,不崩溃
3. 假账号登录 → 错误弹窗 + 日志,按钮状态恢复正常
4. 模式切换 → 课程/单元清空、提示重新登录、参数标签在「正确率」与「时长秒」间切换
5. (有真实账号时)登录 → 课程列表自动加载 → 选中课程 → 单元加载 → 选中单元行自动填入 id
6. 参数输入非法(`abc`、`10,30,40`)点开始 → 警告弹窗说明格式
7. 任务运行中:日志滚动、窗口可拖动不卡死、停止按钮可用,点停止后数秒内收尾并输出统计
8. 任务运行中点关闭 → 确认框;确认后进程退出
9. 双击 `一键启动.bat` → GUI;双击 `命令行启动.bat` → CLI
10. CLI 流程与原版一致:菜单、选课、选单元、参数提示文案

- [ ] **Step 3: 真实账号实测**

由维护者用自己的账号完整跑一次「刷正确率」与「刷时长」(建议先用单个单元小参数验证),确认服务端到账。

- [ ] **Step 4: 收尾提交**

```bash
git add -A
git commit -m "chore: 端到端验收通过"   # 若无文件变动则跳过
git log --oneline
```

Expected: 提交历史干净,全部改动都在 `feature/gui` 分支

---

## Self-Review 记录

- **Spec 覆盖**:共享模块(T1/T2)、两控制器重构+CLI 保留(T3/T4)、GUI+线程模型(T5)、作者信息+双入口(T6)、验收清单含 spec「测试与验收」全部条目(T7)✓
- **占位符扫描**:无 TBD/TODO;所有代码步骤含完整代码 ✓
- **类型一致性**:`run()` 返回键名与 GUI `_after_run`、CLI 包装引用一致;`fetch_units(cid)` 两控制器同签名;`parse_numeric_param` 返回 `int`/`Tuple` 与 `run(param)` 消费方一致 ✓
- **Review Focus**:五条均已钉到 T1/T2/T3 测试与 T7 手工步骤 ✓
