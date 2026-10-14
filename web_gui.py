"""Welearn_helper Web 图形界面(Flask)。刷课任务在后台线程运行,可随时停止。

启动: py -u web_gui.py  →  自动打开浏览器 http://127.0.0.1:17777/
环境变量 WELEARN_NO_OPEN=1 跳过自动开浏览器(冒烟/测试用)。
"""
import os
import threading
import time
import webbrowser
from collections import deque
from typing import Any, Deque, List, Optional, Tuple

from flask import Flask, jsonify, request

import welearn_accuracy
import welearn_common
import welearn_time
from welearn_common import LoginError, WelearnError

BANNER = """**********  Created By Avenshy & SSmJaE  **********
                    Web界面Version:1.0
        原项目作者github https://github.com/Avenshy
        更新维护者github https://github.com/lxbsdsg/Welearn_helper/
***************************************************"""


class AppState:
    """单个应用实例的共享状态。所有变更持锁;log() 可从任意线程调用。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.task = None                      # AccuracyTask 或 TimeTask
        self.mode = "accuracy"
        self.courses: List[dict] = []
        self.units: List[dict] = []
        self.running = False
        self.running_what = "待命"
        self.last_result: Optional[dict] = None
        self.logs: Deque[Tuple[int, str, str]] = deque(maxlen=2000)
        self._log_seq = 0

    def log(self, msg: str):
        with self.lock:
            self._log_seq += 1
            self.logs.append((self._log_seq, time.strftime("%H:%M:%S"), str(msg)))

    def worker_busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _set(self, running: bool, running_what: str):
        with self.lock:
            self.running = running
            self.running_what = running_what


def _format_result(mode: str, result: dict) -> List[str]:
    if mode == "time":
        lines = [f"运行结束!!错误: {result['failed']} 个"]
        for i, err in enumerate(result["errors"], start=1):
            lines.append(f"第{i}个错误:{err}")
        return lines
    return ["方式1: {} 成功, {} 失败".format(result["way1_succeed"], result["way1_failed"]),
            "方式2: {} 成功, {} 失败".format(result["way2_succeed"], result["way2_failed"])]


def create_app():
    """应用工厂,便于测试隔离。返回 (app, state)。"""
    app = Flask(__name__)
    state = AppState()

    @app.route("/")
    def index():
        return INDEX_HTML

    @app.route("/api/login", methods=["POST"])
    def api_login():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        mode = payload.get("mode", "accuracy")
        if mode not in ("accuracy", "time"):
            return jsonify(ok=False, error="未知模式"), 400
        if not username or not password:
            return jsonify(ok=False, error="请输入账号和密码"), 400
        if state.worker_busy():
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        state.stop_event.clear()   # 此前点过"停止"不得阻塞新的登录
        state._set(True, "登录中...")
        cls = welearn_accuracy.AccuracyTask if mode == "accuracy" else welearn_time.TimeTask
        task = cls(username, password, on_log=state.log, stop_event=state.stop_event)
        try:
            task.login()
            courses = task.fetch_courses()
        except (LoginError, WelearnError) as e:
            msg = str(e)
            state._set(False, "待命")
            state.log(f"登录失败:{msg}")
            return jsonify(ok=False, error=msg)
        except Exception as e:
            msg = repr(e)
            state._set(False, "待命")
            state.log(f"登录失败:{msg}")
            return jsonify(ok=False, error=msg)
        with state.lock:
            state.task = task
            state.mode = mode
            state.courses = courses
            state.units = []
            state.last_result = None
        state._set(False, "待命")
        state.log(f"共 {len(courses)} 门课程,请在课程列表中选择")
        return jsonify(ok=True, courses=courses)

    @app.route("/api/units", methods=["POST"])
    def api_units():
        payload = request.get_json(silent=True) or {}
        cid = payload.get("cid")
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if state.worker_busy():
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        state.stop_event.clear()
        state._set(True, "加载单元中...")
        try:
            units = state.task.fetch_units(cid)
        except (LoginError, WelearnError) as e:
            msg = str(e)
            state._set(False, "待命")
            state.log(f"加载单元失败:{msg}")
            return jsonify(ok=False, error=msg)
        except Exception as e:
            msg = repr(e)
            state._set(False, "待命")
            state.log(f"加载单元失败:{msg}")
            return jsonify(ok=False, error=msg)
        with state.lock:
            state.units = units
        state._set(False, "待命")
        state.log(f"已加载 {len(units)} 个单元")
        return jsonify(ok=True, units=units)

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if state.worker_busy():
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        state.stop_event.clear()
        state._set(True, "刷新课程中...")
        try:
            courses = state.task.fetch_courses()
        except (LoginError, WelearnError) as e:
            msg = str(e)
            state._set(False, "待命")
            state.log(f"刷新失败:{msg}")
            return jsonify(ok=False, error=msg)
        except Exception as e:
            msg = repr(e)
            state._set(False, "待命")
            state.log(f"刷新失败:{msg}")
            return jsonify(ok=False, error=msg)
        with state.lock:
            state.courses = courses
            state.units = []
        state._set(False, "待命")
        state.log(f"已刷新,共 {len(courses)} 门课程")
        return jsonify(ok=True, courses=courses)

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        """作废当前登录(切换模式时调用):旧 task 不得再用新参数运行"""
        if state.worker_busy():
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        with state.lock:
            state.task = None
            state.courses = []
            state.units = []
            state.last_result = None
        state.log("已切换模式或退出登录,请重新登录")
        return jsonify(ok=True)

    @app.route("/api/start", methods=["POST"])
    def api_start():
        payload = request.get_json(silent=True) or {}
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if not state.units:
            return jsonify(ok=False, error="请先选择课程加载单元"), 400
        try:
            unit_index = int(payload.get("unit_index", 0))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="单元id必须是数字(0=全部)"), 400
        try:
            param = welearn_common.parse_numeric_param(str(payload.get("param", "")))
        except ValueError as e:
            return jsonify(ok=False, error=f"参数格式错误:{e}\n模式1: 100   模式2: 70,100"), 400
        state.stop_event.clear()
        state._set(True, "任务运行中")
        state.log(f"开始任务:单元id={unit_index} 参数={param}")

        def work():
            try:
                result = state.task.run(unit_index, param)
            except Exception as e:
                msg = repr(e)   # except 块结束会自动删除 e,先取值再使用
                state._set(False, "待命")
                state.log(f"任务异常终止:{msg}")
                return
            with state.lock:
                state.last_result = result
            state._set(False, "待命")
            for line in _format_result(state.mode, result):
                state.log(line)

        with state.lock:   # 忙检查与线程登记同锁,防止并发请求双双通过后重复起线程
            if state.worker_busy():
                return jsonify(ok=False, error="任务正在进行,请先停止"), 409
            state.worker = threading.Thread(target=work, daemon=True)
        state.worker.start()
        return jsonify(ok=True)

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        state.stop_event.set()
        state.log("已请求停止,等待任务收尾...")
        return jsonify(ok=True)

    @app.route("/api/status")
    def api_status():
        try:
            after = int(request.args.get("after", "0"))
        except ValueError:
            after = 0
        with state.lock:
            logs = [list(entry) for entry in state.logs if entry[0] > after]
            return jsonify(running=state.running, running_what=state.running_what,
                           has_task=state.task is not None, mode=state.mode, logs=logs)

    return app, state


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Welearn_helper</title>
<style>
  body { font-family: Consolas, "Microsoft YaHei", monospace; margin: 0 auto; max-width: 960px;
         padding: 12px; background: #f5f5f5; color: #222; }
  pre.banner { text-align: center; background: #111; color: #0f0; padding: 10px;
               border-radius: 6px; overflow-x: auto; }
  fieldset { border: 1px solid #999; border-radius: 6px; margin: 8px 0; background: #fff; }
  legend { font-weight: bold; padding: 0 6px; }
  label { display: inline-block; }
  input[type=text], input[type=password] { width: 150px; }
  select { min-width: 240px; max-width: 100%; }
  .row { margin: 6px 0; }
  button { padding: 4px 18px; margin-right: 6px; }
  #log { height: 320px; overflow-y: scroll; background: #111; color: #0f0;
         padding: 8px; border-radius: 6px; white-space: pre-wrap; font-size: 13px; }
  .status { float: right; color: #b00; font-weight: bold; }
  .dim { color: #888; }
</style>
</head>
<body>
<pre class="banner">**********  Created By Avenshy &amp; SSmJaE  **********
                    Web界面Version:1.0
        原项目作者github https://github.com/Avenshy
        更新维护者github https://github.com/lxbsdsg/Welearn_helper/
***************************************************</pre>

<fieldset>
<legend>登录</legend>
<div class="row">
  账号:<input type="text" id="username">
  密码:<input type="password" id="password">
  <label><input type="radio" name="mode" value="accuracy" checked>刷完成度正确率</label>
  <label><input type="radio" name="mode" value="time">刷学习时长</label>
  <button id="loginBtn">登录</button>
</div>
</fieldset>

<fieldset>
<legend>课程(登录后加载,选中后加载单元)</legend>
<div class="row">
  <select id="courseSel"><option value="">-- 请先登录 --</option></select>
  <button id="refreshBtn">刷新课程</button>
</div>
<div class="row">
  单元:
  <select id="unitSel"><option value="">-- 请先选择课程 --</option></select>
</div>
</fieldset>

<fieldset>
<legend>任务参数</legend>
<div class="row">
  单元id(0=全部):<input type="text" id="unitIndex" value="0" size="4">
  <span id="paramLabel">正确率(100 或 70,100):</span>
  <input type="text" id="param" size="12">
</div>
</fieldset>

<div class="row">
  <button id="startBtn" disabled>开始</button>
  <button id="stopBtn" disabled>停止</button>
  <span class="status" id="statusText">待命</span>
</div>

<fieldset>
<legend>日志</legend>
<div id="log"></div>
</fieldset>

<script>
'use strict';
var lastSeq = 0;
var courses = [];
var $ = function (id) { return document.getElementById(id); };

function apiPost(path, body) {
  return fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify(body || {})})
    .then(function (r) { return r.json(); });
}

function getMode() {
  var checked = document.querySelector('input[name=mode]:checked');
  return checked ? checked.value : 'accuracy';
}

function setControls(running) {
  var busy = running;
  $('loginBtn').disabled = busy;
  $('refreshBtn').disabled = busy;
  $('startBtn').disabled = busy || !window.__hasTask;
  $('stopBtn').disabled = !busy;
}

function fillCourses(list) {
  courses = list || [];
  var sel = $('courseSel');
  sel.innerHTML = '';
  if (!courses.length) {
    sel.appendChild(new Option('-- 无课程 --', ''));
    return;
  }
  for (var i = 0; i < courses.length; i++) {
    var c = courses[i];
    sel.appendChild(new Option('完成度' + c.per + '%  ' + c.name, String(c.cid)));
  }
}

function fillUnits(list) {
  var sel = $('unitSel');
  sel.innerHTML = '';
  var units = list || [];
  if (!units.length) {
    sel.appendChild(new Option('-- 请先选择课程 --', ''));
    return;
  }
  sel.appendChild(new Option('[NO. 0] 按顺序刷全部单元', '0'));
  for (var i = 0; i < units.length; i++) {
    var u = units[i];
    var visible = String(u.visible || 'true').toLowerCase() === 'true';
    sel.appendChild(new Option('[NO.' + (i + 1) + '] ' + (visible ? '[已开放] ' : '![未开放]! ')
                               + u.unitname + '  ' + u.name, String(i + 1)));
  }
}

function appendLogs(entries) {
  var box = $('log');
  var autoscroll = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    if (e[0] > lastSeq) lastSeq = e[0];
    var line = document.createElement('div');
    line.textContent = '[' + e[1] + '] ' + e[2];
    box.appendChild(line);
  }
  if (autoscroll) box.scrollTop = box.scrollHeight;
}

/* 本地提示行:不经过 appendLogs,避免伪造序号污染 lastSeq 导致后续服务端日志全部丢失 */
function appendLocal(text) {
  var box = $('log');
  var line = document.createElement('div');
  line.textContent = text;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

document.querySelectorAll('input[name=mode]').forEach(function (radio) {
  radio.addEventListener('change', function () {
    fillCourses([]);
    fillUnits([]);
    window.__hasTask = false;
    setControls(false);
    $('paramLabel').textContent = getMode() === 'time'
      ? '每练习时长秒(30 或 10,30):' : '正确率(100 或 70,100):';
    appendLocal('已切换模式,请重新登录');
    apiPost('/api/logout', {}).catch(function () {});   // 作废服务端旧 task
  });
});

$('loginBtn').addEventListener('click', function () {
  var username = $('username').value.trim();
  var password = $('password').value;
  if (!username || !password) { alert('请输入账号和密码'); return; }
  apiPost('/api/login', {username: username, password: password, mode: getMode()})
    .then(function (j) {
      if (!j.ok) { alert('登录失败\n' + j.error); return; }
      window.__hasTask = true;
      fillCourses(j.courses);
      setControls(false);
    })
    .catch(function (err) { alert('请求失败:' + err); });
});

$('refreshBtn').addEventListener('click', function () {
  apiPost('/api/refresh', {})
    .then(function (j) {
      if (!j.ok) { alert('刷新失败\n' + j.error); return; }
      fillCourses(j.courses);
      fillUnits([]);
    })
    .catch(function (err) { alert('请求失败:' + err); });
});

$('courseSel').addEventListener('change', function () {
  var cid = $('courseSel').value;
  if (!cid) return;
  apiPost('/api/units', {cid: Number(cid)})
    .then(function (j) {
      if (!j.ok) { alert('加载单元失败\n' + j.error); return; }
      fillUnits(j.units);
    })
    .catch(function (err) { alert('请求失败:' + err); });
});

$('unitSel').addEventListener('change', function () {
  var v = $('unitSel').value;
  if (v !== '') $('unitIndex').value = v;
});

$('startBtn').addEventListener('click', function () {
  var unitIndex = parseInt($('unitIndex').value, 10);
  if (isNaN(unitIndex) || unitIndex < 0) { alert('单元id必须是数字(0=全部)'); return; }
  apiPost('/api/start', {unit_index: unitIndex, param: $('param').value})
    .then(function (j) {
      if (!j.ok) { alert(j.error); return; }
      setControls(true);
      $('statusText').textContent = '任务运行中';
    })
    .catch(function (err) { alert('请求失败:' + err); });
});

$('stopBtn').addEventListener('click', function () {
  apiPost('/api/stop', {}).catch(function (err) { alert('请求失败:' + err); });
});

function poll() {
  fetch('/api/status?after=' + lastSeq)
    .then(function (r) { return r.json(); })
    .then(function (j) {
      appendLogs(j.logs);
      window.__hasTask = j.has_task;
      $('statusText').textContent = j.running_what;
      setControls(j.running);
    })
    .catch(function () { /* 服务未就绪或已关闭,下轮重试 */ });
}

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


def main():
    port = int(os.environ.get("WELEARN_PORT", "17777"))
    url = f"http://127.0.0.1:{port}/"
    print(BANNER)
    print(f"Web 界面地址: {url}  (Ctrl+C 退出)")
    if os.environ.get("WELEARN_NO_OPEN") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True)


app, state = create_app()

if __name__ == '__main__':
    main()
