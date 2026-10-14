"""Welearn_helper Web 图形界面(Flask)。刷课任务在后台线程运行,可随时停止。

启动: py -u web_gui.py  →  自动打开浏览器 http://127.0.0.1:17777/
环境变量 WELEARN_NO_OPEN=1 跳过自动开浏览器(冒烟/测试用)。
"""
import logging
import os
import threading
import time
import webbrowser
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

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
        self.sections_map: Dict[int, List[dict]] = {}   # {单元编号: 小节列表},供小节范围校验
        self.running = False
        self.running_what = "待命"
        self.fetching = False       # 课程/单元/小节拉取进行中(与任务运行区分)
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

    def _hint(self, what: str):
        """只更新状态文本,不改变 running(避免拉取瞬态被误判为任务运行)"""
        with self.lock:
            self.running_what = what

    def _fetch_begin(self, hint: str):
        with self.lock:
            self.fetching = True
        self._hint(hint)

    def _fetch_end(self):
        with self.lock:
            self.fetching = False
        self._hint("待命")


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
        state._hint("登录中...")
        cls = welearn_accuracy.AccuracyTask if mode == "accuracy" else welearn_time.TimeTask
        task = cls(username, password, on_log=state.log, stop_event=state.stop_event)
        try:
            task.login()
            courses = task.fetch_courses()
        except (LoginError, WelearnError) as e:
            state._hint("待命")
            state.log(f"登录失败:{e}")
            return jsonify(ok=False, error=str(e))
        except Exception as e:
            state._hint("待命")
            state.log(f"登录失败:{e!r}")
            return jsonify(ok=False, error=repr(e))
        with state.lock:
            state.task = task
            state.mode = mode
            state.courses = courses
            state.units = []
            state.sections_map = {}
            state.last_result = None
        state._hint("待命")
        state.log(f"共 {len(courses)} 门课程,请在课程列表中选择")
        return jsonify(ok=True, courses=courses)

    @app.route("/api/units", methods=["POST"])
    def api_units():
        payload = request.get_json(silent=True) or {}
        cid = payload.get("cid")
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if state.running:
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        state.stop_event.clear()
        state._fetch_begin("加载单元中...")
        try:
            units = state.task.fetch_units(cid)
        except (LoginError, WelearnError) as e:
            state._fetch_end()
            state.log(f"加载单元失败:{e}")
            return jsonify(ok=False, error=str(e))
        except Exception as e:
            state._fetch_end()
            state.log(f"加载单元失败:{e!r}")
            return jsonify(ok=False, error=repr(e))
        finally:
            state._fetch_end()   # 幂等:正常/异常路径都确保清除拉取标记
        with state.lock:
            state.units = units
        state.log(f"已加载 {len(units)} 个单元")
        return jsonify(ok=True, units=units)

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if state.running:
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        state.stop_event.clear()
        state._fetch_begin("刷新课程中...")
        try:
            courses = state.task.fetch_courses()
        except (LoginError, WelearnError) as e:
            state._fetch_end()
            state.log(f"刷新失败:{e}")
            return jsonify(ok=False, error=str(e))
        except Exception as e:
            state._fetch_end()
            state.log(f"刷新失败:{e!r}")
            return jsonify(ok=False, error=repr(e))
        finally:
            state._fetch_end()
        with state.lock:
            state.courses = courses
        state.log(f"已刷新,共 {len(courses)} 门课程")
        return jsonify(ok=True, courses=courses)

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        """作废当前登录(切换模式时调用):旧 task 不得再用新参数运行"""
        if state.worker_busy() or state.fetching:
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        with state.lock:
            state.task = None
            state.courses = []
            state.units = []
            state.sections_map = {}
            state.last_result = None
        state.log("已切换模式或退出登录,请重新登录")
        return jsonify(ok=True)

    @app.route("/api/sections", methods=["POST"])
    def api_sections():
        payload = request.get_json(silent=True) or {}
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if not state.units:
            return jsonify(ok=False, error="请先选择课程加载单元"), 400
        try:
            unit_index = int(payload.get("unit_index", 0))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="单元序号必须是数字"), 400
        if not 1 <= unit_index <= len(state.units):
            return jsonify(ok=False,
                           error=f"请选择具体单元(1~{len(state.units)});0=全部,无单独小节可显示"), 400
        if state.running:
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        state._fetch_begin("加载小节中...")
        try:
            raw = state.task.fetch_sections(unit_index)
        except (LoginError, WelearnError) as e:
            state._fetch_end()
            state.log(f"加载小节失败:{e}")
            return jsonify(ok=False, error=str(e))
        except Exception as e:
            state._fetch_end()
            state.log(f"加载小节失败:{e!r}")
            return jsonify(ok=False, error=repr(e))
        finally:
            state._fetch_end()
        sections = [{"location": x.get("location", ""),
                     "complete": "未" not in x.get("iscomplete", "未"),
                     "visible": x.get("isvisible", "true") != "false"} for x in raw]
        with state.lock:
            state.sections_map[unit_index] = sections   # 记住编号依据,供 /api/start 校验小节范围
        state.log(f"单元 {unit_index} 共 {len(sections)} 个小节")
        return jsonify(ok=True, sections=sections)

    @app.route("/api/start", methods=["POST"])
    def api_start():
        payload = request.get_json(silent=True) or {}
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if not state.units:
            return jsonify(ok=False, error="请先选择课程加载单元"), 400
        # 兼容旧字段 unit_index/section_index 与新字段 units/sections
        raw_units = payload.get("units", payload.get("unit_index", 0))
        raw_secs = payload.get("sections", payload.get("section_index") or None)
        try:
            all_units, unit_list, section_map = welearn_common.normalize_targets(raw_units, raw_secs)
        except (TypeError, ValueError) as e:
            return jsonify(ok=False, error=str(e)), 400
        if section_map:
            with state.lock:
                known_map = dict(state.sections_map)
            for uno, secs in section_map.items():
                known = known_map.get(uno)
                if known and max(secs) > len(known):
                    return jsonify(ok=False,
                                   error=f"单元{uno}小节id超出范围(1~{len(known)})"), 400
        try:
            param = welearn_common.parse_numeric_param(str(payload.get("param", "")))
        except ValueError as e:
            return jsonify(ok=False, error=f"参数格式错误:{e}\n模式1: 100   模式2: 70,100"), 400
        desc = "全部单元" if all_units else "单元" + ",".join(str(u) for u in unit_list)
        if section_map:
            desc += " 小节" + ",".join(
                f"{u}:[{'/'.join(str(s) for s in secs)}]" for u, secs in section_map.items())
        state.stop_event.clear()
        state._set(True, "任务运行中")
        state.log(f"开始任务:{desc} 参数={param}")

        def work():
            try:
                result = state.task.run(0 if all_units else unit_list, param, section_map)
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
            if state.worker_busy() or state.fetching:
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


# raw 字符串:JS 里的 \n 必须原样送达浏览器;若被 Python 转义成真换行,字符串字面量断行 → SyntaxError
INDEX_HTML = r"""<!DOCTYPE html>
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
  #unitTree { max-height: 360px; overflow-y: auto; background: #fff; border: 1px solid #ccc;
              padding: 4px 8px; font-size: 13px; margin-top: 4px; }
  .unit-row { margin: 3px 0; }
  .unit-row > label { font-weight: bold; display: block; }
  .sec-box { margin-left: 30px; margin-top: 2px; }
  .sec-row { display: block; font-weight: normal; }
  .sec-done { color: #0a7d25; }
  .sec-todo { color: #b00; }
  .sec-hidden { color: #999; }
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
</fieldset>

<fieldset>
<legend>单元与小节(勾选要刷的单元;勾选单元后展开小节,勾选小节=只刷勾选的,不勾=刷整个单元)</legend>
<div class="row">
  <label><input type="checkbox" id="allUnits"> 全部单元(忽略下方勾选)</label>
</div>
<div id="unitTree"></div>
</fieldset>

<fieldset>
<legend>任务参数</legend>
<div class="row">
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

function fillCourses(list, keepSelection) {
  courses = list || [];
  var sel = $('courseSel');
  var prev = keepSelection ? sel.value : '';
  sel.innerHTML = '';
  if (!courses.length) {
    sel.appendChild(new Option('-- 无课程 --', ''));
    return;
  }
  for (var i = 0; i < courses.length; i++) {
    var c = courses[i];
    sel.appendChild(new Option('完成度' + c.per + '%  ' + c.name, String(c.cid)));
  }
  if (prev) {   // 刷新后恢复原选择,避免用户重新点一遍
    for (var k = 0; k < sel.options.length; k++) {
      if (sel.options[k].value === prev) { sel.value = prev; break; }
    }
  }
}

function renderTree(units) {
  var tree = $('unitTree');
  tree.innerHTML = '';
  var list = units || [];
  if (!list.length) {
    var hint = document.createElement('div');
    hint.className = 'dim';
    hint.textContent = '-- 请先选择课程加载单元 --';
    tree.appendChild(hint);
    return;
  }
  for (var i = 0; i < list.length; i++) {
    var u = list[i];
    var no = i + 1;
    var visible = String(u.visible || 'true').toLowerCase() === 'true';
    var row = document.createElement('div');
    row.className = 'unit-row';
    var label = document.createElement('label');
    if (!visible) label.className = 'sec-hidden';   // 未开放单元灰显,仍可勾(其小节会被跳过)
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'unit-cb';
    cb.dataset.no = String(no);
    cb.addEventListener('change', function () { onUnitToggled(this); });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(
      ' [NO.' + no + '] ' + (visible ? '[已开放] ' : '![未开放]! ') + u.unitname + '  ' + u.name));
    var secBox = document.createElement('div');
    secBox.className = 'sec-box';
    secBox.dataset.no = String(no);
    secBox.style.display = 'none';
    row.appendChild(label);
    row.appendChild(secBox);
    tree.appendChild(row);
  }
}

function renderSecs(no, sections) {
  var box = document.querySelector('.sec-box[data-no="' + no + '"]');
  if (!box) return;
  box.innerHTML = '';
  if (!sections || !sections.length) {
    var d = document.createElement('div');
    d.className = 'dim';
    d.textContent = '-- 该单元无小节数据 --';
    box.appendChild(d);
    return;
  }
  for (var i = 0; i < sections.length; i++) {
    var s = sections[i];
    var secNo = i + 1;   // 编号即小节参数依据
    var label = document.createElement('label');
    label.className = 'sec-row ' + (!s.visible ? 'sec-hidden'
                                     : (s.complete ? 'sec-done' : 'sec-todo'));
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'sec-cb';
    cb.dataset.no = String(no);
    cb.dataset.sec = String(secNo);
    if (!s.visible) { cb.disabled = true; cb.dataset.baseDisabled = '1'; }   // 未开放小节不可刷
    label.appendChild(cb);
    label.appendChild(document.createTextNode(
      ' ' + secNo + '. ' + (!s.visible ? '[未开放] ' : (s.complete ? '[已完成] ' : '[未完成] '))
      + s.location));
    box.appendChild(label);
  }
}

/* 勾选单元时懒加载其小节;done 回调用于恢复勾选场景(加载完成后再打勾) */
function ensureSecs(no, done) {
  var box = document.querySelector('.sec-box[data-no="' + no + '"]');
  if (!box) { if (done) done(); return; }
  if (box.childNodes.length) { if (done) done(); return; }   // 已加载(或加载中)不重复请求
  var loading = document.createElement('div');
  loading.className = 'dim';
  loading.textContent = '-- 小节加载中... --';
  box.appendChild(loading);
  apiPost('/api/sections', {unit_index: no})
    .then(function (j) {
      if (!j.ok) { box.innerHTML = ''; appendLocal('加载小节失败:' + j.error); return; }
      renderSecs(no, j.sections);
      applyAllUnits();
      if (done) done();
    })
    .catch(function (err) { box.innerHTML = ''; appendLocal('请求失败:' + err); });
}

function onUnitToggled(cb) {
  var box = document.querySelector('.sec-box[data-no="' + cb.dataset.no + '"]');
  if (!box) return;
  if (cb.checked) {
    box.style.display = '';
    ensureSecs(Number(cb.dataset.no));
  } else {
    box.style.display = 'none';
  }
}

function applyAllUnits() {
  var on = $('allUnits').checked;
  document.querySelectorAll('.unit-cb').forEach(function (cb) { cb.disabled = on; });
  document.querySelectorAll('.sec-cb').forEach(function (sc) {
    sc.disabled = on || sc.dataset.baseDisabled === '1';
  });
  $('unitTree').style.opacity = on ? '0.5' : '';
}

function resetTree() {
  $('allUnits').checked = false;
  applyAllUnits();
  renderTree([]);
}

/* 从界面收集任务目标:{allUnits, units:[编号], sections:{编号:[小节编号]}} */
function buildSelection() {
  var sel = {allUnits: $('allUnits').checked, units: [], sections: {}};
  if (sel.allUnits) return sel;
  var byNo = {};
  document.querySelectorAll('.unit-cb').forEach(function (cb) {
    if (cb.checked) { byNo[cb.dataset.no] = true; sel.units.push(Number(cb.dataset.no)); }
  });
  document.querySelectorAll('.sec-cb').forEach(function (sc) {
    if (!sc.checked || sc.disabled) return;
    if (!byNo[sc.dataset.no]) return;   // 单元未勾选时其小节勾选无效
    if (!sel.sections[sc.dataset.no]) sel.sections[sc.dataset.no] = [];
    sel.sections[sc.dataset.no].push(Number(sc.dataset.sec));
  });
  return sel;
}

/* 刷新/重登后恢复勾选:单元直接打勾,小节需重新拉取后再打勾 */
function restoreSelection(sel) {
  if (!sel) return;
  $('allUnits').checked = !!sel.allUnits;
  applyAllUnits();
  var want = {};
  (sel.units || []).forEach(function (no) { want[no] = true; });
  document.querySelectorAll('.unit-cb').forEach(function (cb) {
    cb.checked = !!want[cb.dataset.no];
    if (cb.checked) onUnitToggled(cb);
  });
  var secs = sel.sections || {};
  Object.keys(secs).forEach(function (noStr) {
    var cb = document.querySelector('.unit-cb[data-no="' + noStr + '"]');
    if (!cb || !cb.checked) return;
    ensureSecs(Number(noStr), function () {
      document.querySelectorAll('.sec-cb[data-no="' + noStr + '"]').forEach(function (sc) {
        sc.checked = secs[noStr].indexOf(Number(sc.dataset.sec)) >= 0;
      });
    });
  });
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

/* 刷新课程(保留选择);若已选课程则连单元一起刷新并恢复单元/小节勾选 */
function refreshAll(kind) {
  var prevSel = buildSelection();
  apiPost('/api/refresh', {})
    .then(function (j) {
      if (!j.ok) { if (kind === 'manual') alert('刷新失败\n' + j.error); return; }
      fillCourses(j.courses, true);
      if (!$('courseSel').value) { renderTree([]); return; }
      apiPost('/api/units', {cid: Number($('courseSel').value)})
        .then(function (j2) {
          if (!j2.ok) { renderTree([]); return; }
          renderTree(j2.units);
          restoreSelection(prevSel);
        })
        .catch(function () {});
    })
    .catch(function (err) {
      if (kind === 'manual') alert('请求失败:' + err);
    });
}

document.querySelectorAll('input[name=mode]').forEach(function (radio) {
  radio.addEventListener('change', function () {
    fillCourses([]);
    resetTree();
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
  var prevCourse = $('courseSel').value;
  var prevSel = buildSelection();
  apiPost('/api/login', {username: username, password: password, mode: getMode()})
    .then(function (j) {
      if (!j.ok) { alert('登录失败\n' + j.error); return; }
      window.__hasTask = true;
      fillCourses(j.courses, true);
      if (prevCourse && $('courseSel').value === prevCourse
          && (prevSel.units.length || prevSel.allUnits)) {
        /* 重新登录且课程未变:自动恢复单元与小节勾选,无需重选 */
        apiPost('/api/units', {cid: Number(prevCourse)})
          .then(function (j2) {
            if (!j2.ok) { renderTree([]); return; }
            renderTree(j2.units);
            restoreSelection(prevSel);
          })
          .catch(function () { renderTree([]); });
      } else {
        /* 首次登录:下拉默认高亮第一门课,但浏览器不会再触发 change——
           必须主动加载其单元,否则"看起来选了课却没有单元" */
        var firstCid = $('courseSel').value;
        if (firstCid) {
          apiPost('/api/units', {cid: Number(firstCid)})
            .then(function (j2) { renderTree(j2.ok ? j2.units : []); })
            .catch(function () { renderTree([]); });
        } else {
          renderTree([]);
        }
      }
      setControls(false);
    })
    .catch(function (err) { alert('请求失败:' + err); });
});

$('refreshBtn').addEventListener('click', function () {
  refreshAll('manual');
});

$('courseSel').addEventListener('change', function () {
  var cid = $('courseSel').value;
  if (!cid) return;
  apiPost('/api/units', {cid: Number(cid)})
    .then(function (j) {
      if (!j.ok) { alert('加载单元失败\n' + j.error); renderTree([]); return; }
      renderTree(j.units);
    })
    .catch(function (err) { alert('请求失败:' + err); });
});

$('allUnits').addEventListener('change', applyAllUnits);

$('startBtn').addEventListener('click', function () {
  var sel = buildSelection();
  if (!sel.allUnits && !sel.units.length) {
    alert('请至少勾选一个单元,或勾选"全部单元"');
    return;
  }
  var body = {units: sel.allUnits ? 0 : sel.units,
              sections: sel.allUnits ? null : sel.sections,
              param: $('param').value};
  apiPost('/api/start', body)
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

var wasRunning = false;

function poll() {
  fetch('/api/status?after=' + lastSeq)
    .then(function (r) { return r.json(); })
    .then(function (j) {
      appendLogs(j.logs);
      if (wasRunning && !j.running) {
        /* 任务刚结束:课程完成度与小节状态已变,自动刷新(保留全部选择) */
        appendLocal('[界面] 任务结束,自动刷新课程与小节状态...');
        refreshAll('task-end');
      }
      wasRunning = j.running;
      window.__hasTask = j.has_task;
      $('statusText').textContent = j.running_what;
      setControls(j.running);
    })
    .catch(function () { /* 服务未就绪或已关闭,下轮重试 */ });
}

renderTree([]);
poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


class _QuietStatusPoll(logging.Filter):
    """控制台隐藏 /api/status 轮询访问行(每秒一条会刷屏),其余访问行保留"""

    def filter(self, record):
        return "/api/status" not in record.getMessage()


def main():
    logging.getLogger("werkzeug").addFilter(_QuietStatusPoll())
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
