"""Welearn_helper Web 图形界面(Flask)。刷课任务在后台线程运行,可随时停止。

启动: py -u web_gui.py  →  自动打开浏览器 http://127.0.0.1:17777/
环境变量 WELEARN_NO_OPEN=1 跳过自动开浏览器(冒烟/测试用)。
"""
import logging
import os
import re
import threading
import time
import webbrowser
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any, Deque, Dict, List, Optional, Tuple

from flask import Flask, cli as flask_cli, jsonify, request

import welearn_accuracy
import welearn_common
import welearn_flow
import welearn_time
from version import __version__
from welearn_common import LoginError, WelearnError

_TASK_MODES = ("accuracy", "time", "queue")


def _task_class(mode):
    """按模式取控制器类(请求时取,便于测试 monkeypatch)"""
    return {"accuracy": welearn_accuracy.AccuracyTask,
            "time": welearn_time.TimeTask,
            "queue": welearn_flow.FlowTask}[mode]


BANNER = f"""**********  Created By Avenshy & SSmJaE  **********
                    Web界面Version:{__version__}
        原项目作者github https://github.com/Avenshy
        更新维护者github https://github.com/YZBRH
                        https://github.com/lxbsdsg
***************************************************"""

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_logs")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class AppState:
    """单个应用实例的共享状态。所有变更持锁;log() 可从任意线程调用。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.task = None                      # AccuracyTask / TimeTask / FlowTask
        self.mode = "accuracy"
        self.courses: List[dict] = []
        self.units: List[dict] = []
        self.sections_map: Dict[int, List[dict]] = {}   # {单元编号: 小节列表},供小节范围校验
        self.running = False
        self.running_what = "待命"
        self.fetching = False       # 课程/单元/小节拉取进行中(与任务运行区分)
        self.skip_event = threading.Event()   # 顺序任务:跳过当前课程
        self.queue: List[dict] = []           # 顺序任务:本次队列快照
        self.queue_summary: Optional[dict] = None
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


def _hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _format_result(mode: str, result: dict) -> List[str]:
    if mode == "time":
        lines = [f"运行结束!!错误: {result['failed']} 个"]
        for i, err in enumerate(result["errors"], start=1):
            lines.append(f"第{i}个错误:{err}")
        return lines
    if mode == "queue":
        lines = [f"顺序任务结束:完成 {result['done']} 节 · 跳过 {result['skipped']} 节 · "
                 f"失败 {result['failed']} 节 · 用时 {_hms(result['elapsed'])}"]
        for i, course in enumerate(result.get("courses", []), start=1):
            lines.append(f"  {i}. NO.{course['no']} {course['name']} [{course['status']}] "
                         f"完成 {course['done']} / 跳过 {course['skipped']} / "
                         f"失败 {course['failed']}")
            for err in course.get("errors", []):
                lines.append(f"     - {err}")
        return lines
    return ["方式1: {} 成功, {} 失败".format(result["way1_succeed"], result["way1_failed"]),
            "方式2: {} 成功, {} 失败".format(result["way2_succeed"], result["way2_failed"])]



def create_app():
    """应用工厂,便于测试隔离。返回 (app, state)。"""
    app = Flask(__name__)
    state = AppState()

    def _selected_courses(payload):
        """把界面传来的 cid 列表换成课程快照,并按课程编号(课程列表序号)升序"""
        raw = payload.get("cids")
        if isinstance(raw, str):
            raw = [x for x in raw.split(",") if x.strip()]
        if not raw:
            raise ValueError("请至少选择一门课程")
        index = {str(course.get("cid")): i for i, course in enumerate(state.courses)}
        picked, seen = [], set()
        for item in raw:
            cid = str(item).strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            if cid not in index:
                raise ValueError(f"课程 {cid} 不在当前课程列表中,请先刷新课程")
            course = state.courses[index[cid]]
            picked.append({"no": index[cid] + 1, "cid": cid,
                           "name": course.get("name", ""), "per": course.get("per", 0)})
        picked.sort(key=lambda course: course["no"])   # 按课程编号顺序
        return picked

    def _queue_params(payload):
        def span(key, label):
            raw = payload.get(key)
            if raw in (None, ""):
                raise ValueError(f"请填写{label}")
            return welearn_flow.to_span(raw)

        def bounds(value):
            return value if isinstance(value, tuple) else (value, value)

        accuracy = span("accuracy", "正确率")
        duration = span("duration", "学习时长")
        pause = welearn_flow.to_span(payload.get("pause") or list(welearn_flow.DEFAULT_PAUSE))
        acc_lo, acc_hi = bounds(accuracy)
        if acc_lo < 0:
            raise ValueError("正确率不能为负数")
        if acc_hi > 100:
            raise ValueError("正确率不能超过 100")
        for value, label in ((duration, "学习时长"), (pause, "节间休息")):
            lo, _hi = bounds(value)
            if lo < 0:
                raise ValueError(f"{label}不能为负数")
        return {"accuracy": accuracy, "duration": duration, "pause": pause,
                "skip_done": bool(payload.get("skip_done", True))}


    @app.route("/")
    def index():
        return INDEX_HTML

    @app.route("/api/login", methods=["POST"])
    def api_login():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        mode = payload.get("mode", "accuracy")
        if mode not in _TASK_MODES:
            return jsonify(ok=False, error="未知模式"), 400
        if not username or not password:
            return jsonify(ok=False, error="请输入账号和密码"), 400
        if state.worker_busy():
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        state.stop_event.clear()   # 此前点过"停止"不得阻塞新的登录
        state.skip_event.clear()
        state._hint("登录中...")
        cls = _task_class(mode)
        kwargs = {"on_log": state.log, "stop_event": state.stop_event}
        if mode == "queue":
            kwargs["skip_event"] = state.skip_event   # 只有 FlowTask 认这个参数
        task = cls(username, password, **kwargs)
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
            state.queue = []
            state.queue_summary = None
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
        """作废当前登录:旧 task 不得再用新参数运行"""
        if state.worker_busy() or state.fetching:
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        with state.lock:
            state.task = None
            state.courses = []
            state.units = []
            state.sections_map = {}
            state.last_result = None
        state.log("已退出登录,请重新登录")
        return jsonify(ok=True)

    @app.route("/api/mode", methods=["POST"])
    def api_mode():
        payload = request.get_json(silent=True) or {}
        mode = payload.get("mode")
        if mode not in _TASK_MODES:
            return jsonify(ok=False, error="未知模式"), 400

        with state.lock:
            if state.mode == mode:
                return jsonify(ok=True, mode=mode)

            if state.task is None:
                state.mode = mode
            else:
                if state.running or state.worker_busy() or state.fetching:
                    return jsonify(ok=False, error="任务正在进行,请先停止"), 409

                old_task = state.task
                cls = _task_class(mode)
                kwargs = {"on_log": state.log, "stop_event": state.stop_event,
                          "session": old_task.session}
                if mode == "queue":
                    kwargs["skip_event"] = state.skip_event
                task = cls(old_task.username, old_task.password, **kwargs)
                for attribute in ("cid", "uid", "classid"):
                    setattr(task, attribute, getattr(old_task, attribute, ""))
                task.units = list(getattr(old_task, "units", []) or [])

                state.task = task
                state.mode = mode
                state.queue = []
                state.queue_summary = None
                state.last_result = None

        mode_name = {"queue": "顺序刷取", "time": "刷学习时长"}.get(mode, "刷完成度正确率")
        state.log(f"已切换到{mode_name}模式,无需重新登录")
        return jsonify(ok=True, mode=mode)

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
            state.stop_event.clear()
            # 通过忙检后才置运行状态:409 拒绝路径若提前置位,running 永远无人清除,界面卡死
            state.running = True
            state.running_what = "任务运行中"
            state.worker = threading.Thread(target=work, daemon=True)
        state.log(f"开始任务:{desc} 参数={param}")
        state.worker.start()
        return jsonify(ok=True)

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        state.stop_event.set()
        state.log("已请求停止,等待任务收尾...")
        return jsonify(ok=True)

    @app.route("/api/queue/preview", methods=["POST"])
    def api_queue_preview():
        """只读侦察:统计每门课程的小节数并给出耗时预估(不写入任何数据)"""
        payload = request.get_json(silent=True) or {}
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if state.running or state.worker_busy() or state.fetching:
            return jsonify(ok=False, error="任务正在进行,请先停止"), 409
        try:
            picked = _selected_courses(payload)
            params = _queue_params(payload)
            all_units, unit_list, section_map = welearn_common.normalize_targets(
                payload.get("units", 0), payload.get("sections") or None)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        state.stop_event.clear()
        state._fetch_begin("预估耗时中...")
        # 用临时任务实例侦察,避免污染 state.task 的 cid/units 上下文
        probe = welearn_flow.FlowTask(getattr(state.task, "username", ""),
                                      getattr(state.task, "password", ""),
                                      on_log=lambda msg: None,
                                      stop_event=state.stop_event,
                                      session=state.task.session)
        rows, total_seconds = [], 0.0
        try:
            for course in picked:
                try:
                    units = probe.fetch_units(course["cid"])
                except (LoginError, WelearnError) as e:
                    rows.append({**course, "units": 0, "sections": 0, "error": str(e)})
                    continue
                count = 0
                indices = (range(len(units)) if all_units
                           else [uno - 1 for uno in unit_list if 0 < uno <= len(units)])
                for unit_index in indices:
                    unit = units[unit_index]
                    if str(unit.get("visible", "true")).lower() == "false":
                        continue
                    try:
                        sections = probe.fetch_sections(unit_index + 1)
                    except Exception:
                        continue
                    wanted = section_map.get(unit_index + 1) if section_map else None
                    count += len([no for no in range(1, len(sections) + 1)
                                  if not wanted or no in wanted])
                per_section = (welearn_flow.avg_of(params["duration"])
                               + welearn_flow.avg_of(params["pause"])
                               + 4 * welearn_flow.DEFAULT_REQUEST_INTERVAL)
                total_seconds += count * per_section
                rows.append({**course, "units": len(units), "sections": count, "error": ""})
        finally:
            state._fetch_end()

        sections = sum(row["sections"] for row in rows)
        state.log(f"预估完成:{len(picked)} 门课程共 {sections} 个小节,"
                  f"按当前参数预计 {_hms(total_seconds)}")
        return jsonify(ok=True, rows=rows, sections=sections,
                       seconds=int(total_seconds), eta=_hms(total_seconds))

    @app.route("/api/queue/start", methods=["POST"])
    def api_queue_start():
        payload = request.get_json(silent=True) or {}
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if not state.courses:
            return jsonify(ok=False, error="请先登录并刷新课程"), 400
        try:
            picked = _selected_courses(payload)
            params = _queue_params(payload)
            all_units, unit_list, section_map = welearn_common.normalize_targets(
                payload.get("units", 0), payload.get("sections") or None)
        except ValueError as e:
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
            jobs = [welearn_flow.normalize_job(
                {"no": course["no"], "cid": course["cid"], "name": course["name"],
                 "units": 0 if all_units else unit_list,
                 "sections": section_map or None,
                 "accuracy": params["accuracy"], "duration": params["duration"],
                 "pause": params["pause"], "skip_done": params["skip_done"]})
                for course in picked]
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        def work():
            try:
                summary = state.task.run_queue(jobs)
            except Exception as e:
                msg = repr(e)   # except 块结束会自动删除 e,先取值再使用
                state._set(False, "待命")
                state.log(f"顺序任务异常终止:{msg}")
                return
            with state.lock:
                state.queue_summary = summary
                state.last_result = summary
            state._set(False, "待命")
            for line in _format_result("queue", summary):
                state.log(line)

        switched = False
        with state.lock:   # 忙检查与线程登记同锁
            if state.worker_busy() or state.fetching:
                return jsonify(ok=False, error="任务正在进行,请先停止"), 409
            if state.mode != "queue" or not hasattr(state.task, "run_queue"):
                old_task = state.task
                cls = _task_class("queue")
                kwargs = {"on_log": state.log, "stop_event": state.stop_event,
                          "session": old_task.session, "skip_event": state.skip_event}
                state.task = cls(getattr(old_task, "username", ""),
                                 getattr(old_task, "password", ""), **kwargs)
                for attribute in ("cid", "uid", "classid"):
                    setattr(state.task, attribute, getattr(old_task, attribute, ""))
                state.task.units = list(getattr(old_task, "units", []) or [])
                state.mode = "queue"
                switched = True
            state.stop_event.clear()
            state.skip_event.clear()
            state.queue = jobs
            state.queue_summary = None
            state.running = True
            state.running_what = "顺序任务运行中"
            state.worker = threading.Thread(target=work, daemon=True)

        if switched:
            state.log("已切换到顺序刷取模式,无需重新登录")
        scope = "全部单元" if all_units else "单元" + ",".join(str(u) for u in unit_list)
        if section_map:
            scope += " 小节" + ",".join(
                f"{uno}:[{'/'.join(str(s) for s in secs)}]"
                for uno, secs in section_map.items())
        state.log(f"开始顺序任务:共 {len(jobs)} 门课程(按课程编号顺序) · {scope} · "
                  f"正确率 {params['accuracy']} · 学习时长 {params['duration']}")
        state.worker.start()
        return jsonify(ok=True, total=len(jobs), scope=scope,
                       queue=[{"no": job["no"], "name": job["name"]} for job in jobs])

    @app.route("/api/queue/skip", methods=["POST"])
    def api_queue_skip():
        if state.task is None:
            return jsonify(ok=False, error="请先登录"), 400
        if not state.running:
            return jsonify(ok=False, error="当前没有运行中的任务"), 409
        state.skip_event.set()
        state.log("已请求跳过当前课程,队列将执行下一门")
        return jsonify(ok=True)

    @app.route("/api/status")
    def api_status():
        try:
            after = int(request.args.get("after", "0"))
        except ValueError:
            after = 0
        with state.lock:
            logs = [list(entry) for entry in state.logs if entry[0] > after]
            queue = None
            if state.queue:
                queue = {"total": len(state.queue),
                         "names": [job.get("name", "") for job in state.queue],
                         "summary": state.queue_summary}
                queue.update(dict(getattr(state.task, "progress", None) or {}))
            return jsonify(running=state.running, running_what=state.running_what,
                           has_task=state.task is not None, mode=state.mode,
                           logs=logs, queue=queue)

    return app, state


# raw 字符串:JS 里的 \n 必须原样送达浏览器;若被 Python 转义成真换行,字符串字面量断行 → SyntaxError
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Welearn Helper · 刷课工作台</title>
<style>
  :root {
    --bg: #EDF0F4;            /* 工作台底色 */
    --panel: #FFFFFF;         /* 配置区面板 */
    --ops: #141C26;           /* 执行区(运行条+控制台)深底 */
    --ops-line: #2C3A49;
    --ink: #1C2836;
    --ink-2: #56677A;
    --ink-3: #75828F;
    --line: #D8DEE6;
    --line-soft: #E8ECF1;
    --hover: #F4F7FA;
    --blue: #3D6FA5;          /* 选择/聚焦 */
    --run: #12753F;           /* 开始 */
    --run-h: #0E6134;
    --danger: #C13B3B;        /* 停止 */
    --ops-ink: #E1E8EF;       /* 执行区浅字 */
    --ops-dim: #8FA0B2;
    --console-dim: #6E8096;
    --mono: "Cascadia Mono", Consolas, "Courier New", monospace;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body { font: 14px/1.55 "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
         background: var(--bg); color: var(--ink); }
  .shell { max-width: 1240px; margin: 0 auto; padding: 18px 20px 24px; }

  /* 顶栏:品牌 + 出处 + 状态灯 */
  .topbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
            background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
            padding: 12px 18px; margin-bottom: 14px; }
  .brand { display: flex; align-items: baseline; gap: 12px; margin-right: auto; min-width: 0; }
  .brand h1 { font-size: 16px; font-weight: 700; margin: 0; letter-spacing: .2px; }
  .brand .sub { font-size: 12px; color: var(--ink-3); }
  .brand .sub a { color: var(--blue); text-decoration: none; }
  .brand .sub a:hover { text-decoration: underline; }
  .pill { display: inline-flex; align-items: center; gap: 8px; height: 28px; padding: 0 13px;
          border: 1px solid var(--line); border-radius: 14px; font-size: 12.5px; font-weight: 600;
          color: var(--ink-2); background: #fff; white-space: nowrap; }
  .pill .dot { width: 7px; height: 7px; border-radius: 50%; background: #93A2B1; }
  .pill.running { color: #8C5800; background: #FBF2DE; border-color: #EBD5A4; }
  .pill.running .dot { background: #D9940A; animation: blink 1.4s ease-in-out infinite; }
  @keyframes blink { 50% { opacity: .35; } }

  /* 双栏工作区:左=配置(浅),右=执行(深) */
  .workspace { display: grid; grid-template-columns: 400px minmax(0, 1fr); gap: 14px;
               align-items: stretch; }
  /* align-self:start:左栏保持自然高度(任务参数底即两栏底),供日志区对齐,
     不随右栏日志内容被拉伸 */
  .rail { display: flex; flex-direction: column; gap: 14px; min-width: 0; align-self: start; }
  .ops-col { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
  .p-head { display: flex; align-items: center; gap: 9px; padding: 10px 14px;
            border-bottom: 1px solid var(--line-soft); }
  .p-step { width: 20px; height: 20px; flex: none; border: 1px solid var(--line); border-radius: 5px;
            display: grid; place-items: center; font: 600 11px/1 var(--mono); color: var(--ink-3); }
  .p-title { font-size: 13.5px; font-weight: 650; }
  .p-body { padding: 12px 14px 14px; }

  /* 表单 */
  .field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
  .f-label { font-size: 12px; color: var(--ink-2); }
  input[type=text], input[type=password], select {
    height: 34px; padding: 0 10px; border: 1px solid var(--line); border-radius: 6px;
    background: #fff; color: var(--ink); font: inherit; min-width: 0; width: 100%;
  }
  input:focus, select:focus { outline: none; border-color: var(--blue);
                              box-shadow: 0 0 0 3px rgba(61, 111, 165, .15); }
  input[type=checkbox], input[type=radio] { accent-color: var(--blue); width: 15px; height: 15px;
                                             flex: none; }
  .radio { display: inline-flex; align-items: center; gap: 7px; margin-right: 16px; cursor: pointer; }
  .row-inline { display: flex; align-items: center; gap: 8px; }
  .grow { flex: 1; }
  .tip { margin: 0 0 8px; font-size: 12px; color: var(--ink-3); }

  /* 按钮 */
  .btn { height: 34px; padding: 0 16px; border-radius: 6px; border: 1px solid var(--line);
         background: #fff; color: var(--ink); font-family: inherit; font-size: 13.5px;
         font-weight: 600; cursor: pointer; white-space: nowrap; }
  .btn:hover:not(:disabled) { border-color: #B9C4D0; background: var(--hover); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
  .btn-blue { border-color: var(--blue); color: var(--blue); }
  .btn-blue:hover:not(:disabled) { border-color: #33608F; background: #F0F5FA; }
  .btn-block { width: 100%; margin-top: 12px; }
  .btn-lg { height: 36px; padding: 0 22px; }
  .btn-run { background: var(--run); border-color: var(--run); color: #fff; }
  .btn-run:hover:not(:disabled) { background: var(--run-h); border-color: var(--run-h); }
  .btn-stop { background: transparent; border-color: #7E4A4A; color: #E89B9B; }
  .btn-stop:hover:not(:disabled) { background: #241A1A; border-color: #9C5B5B; }

  /* 单元/小节树 */
  .all-row { display: flex; align-items: center; gap: 8px; padding-bottom: 8px; margin-bottom: 8px;
             border-bottom: 1px dashed var(--line-soft); font-size: 13px; color: var(--ink-2);
             cursor: pointer; }
  #unitTree { max-height: 430px; overflow-y: auto; border: 1px solid var(--line-soft);
              border-radius: 6px; padding: 6px; }
  .unit-row { margin: 1px 0 5px; }
  .unit-row > label { display: flex; align-items: center; gap: 9px; padding: 6px 7px;
                      border-radius: 6px; cursor: pointer; user-select: none; }
  .unit-row > label:hover { background: var(--hover); }
  .u-no { flex: none; font: 600 11px var(--mono); color: var(--ink-3); letter-spacing: .3px; }
  .u-name { font-weight: 600; font-size: 13px; overflow-wrap: anywhere; }
  .unit-off .u-name { color: var(--ink-3); }
  .sec-box { margin: 1px 0 4px 31px; padding-left: 12px; border-left: 2px solid var(--line-soft); }
  .sec-row { display: flex; align-items: center; gap: 8px; padding: 3px 6px; border-radius: 5px;
             font-size: 13px; cursor: pointer; }
  .sec-row:hover { background: var(--hover); }
  .sec-name { color: var(--ink-2); overflow-wrap: anywhere; }
  .sec-hidden .sec-name { color: var(--ink-3); }
  .tag { flex: none; font-size: 11px; font-weight: 600; line-height: 1; padding: 3px 7px;
         border-radius: 4px; }
  .tag-done { color: #0E6B3C; background: #E7F4EC; }
  .tag-todo { color: #A33333; background: #FAECEC; }
  .tag-off { color: #6A7683; background: #EEF1F4; }
  .dim { color: var(--ink-3); font-size: 12.5px; padding: 4px 6px; }

  /* 只给「任务参数」面板里直接堆叠的字段加间距;写成后代选择器会连带命中
     会话面板 .grid-2 里的第二个 field,把"密码"整列推下去造成错位 */
  .p-body > .field + .field { margin-top: 10px; }
  .btn-skip { background: transparent; border-color: #4A5D70; color: #B9C9D8; }
  .btn-skip:hover:not(:disabled) { background: #1B2632; border-color: #647D93; color: var(--ops-ink); }

  /* 执行区:深色,呼应控制台 */
  .ops { background: var(--ops); border-color: var(--ops-line); color: var(--ops-ink); }
  .runbar { padding: 12px 16px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .run-summary { display: flex; align-items: baseline; gap: 10px; min-width: 0; flex: 1; }
  .rs-k { flex: none; font-size: 11px; color: var(--ops-dim); font-weight: 600; }
  #runSummary { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .run-actions { display: flex; gap: 10px; margin-left: auto; }
  .console { flex: 1; display: flex; flex-direction: column; min-height: 420px; overflow: hidden; }
  .p-head-d { border-bottom: 1px solid var(--ops-line); }
  .p-head-d .p-title { color: #D5DEE7; }
  .btn-ghost-d { height: 26px; padding: 0 11px; margin-left: auto; border-radius: 5px;
                 border: 1px solid #3A4A5A; background: transparent; color: var(--ops-dim);
                 font-family: inherit; font-size: 12px; font-weight: 600; cursor: pointer; }
  .btn-ghost-d:hover { color: var(--ops-ink); border-color: #54687C; background: #1B2632; }
  .btn-ghost-d:focus-visible { outline: 2px solid #7FA3CC; outline-offset: 2px; }
  #log { flex: 1; overflow-y: auto; padding: 10px 16px 14px; white-space: pre-wrap;
         font: 12.5px/1.7 var(--mono); color: #C6D1DC; border-radius: 0 0 8px 8px; }
  #log .t { color: var(--console-dim); }

  @media (max-width: 940px) {
    .workspace { grid-template-columns: 1fr; }
    .console { min-height: 380px; }
  }
  @media (max-width: 460px) {
    .grid-2 { grid-template-columns: 1fr; }   /* 极窄屏:账号/密码上下排布 */
  }
  @media (prefers-reduced-motion: reduce) {
    .pill.running .dot { animation: none; }
  }
</style>
</head>
<body>
<div class="shell">

<header class="topbar">
  <div class="brand">
    <h1>Welearn Helper</h1>
    <span class="sub">刷课工作台(原项目 <a href="https://github.com/Avenshy" target="_blank"
      rel="noreferrer">Avenshy</a> &amp; SSmJaE,由 <a
      href="https://github.com/YZBRH/" target="_blank"
      rel="noreferrer">YZBRH</a>、<a
      href="https://github.com/lxbsdsg/" target="_blank"
      rel="noreferrer">lxbsdsg</a> 维护)</span>
  </div>
  <span class="pill" id="statusPill"><span class="dot"></span><span id="statusText">待命</span></span>
</header>

<div class="workspace">

  <div class="rail">
    <section class="panel">
      <div class="p-head"><span class="p-step">1</span><span class="p-title">会话</span></div>
      <div class="p-body">
        <div class="grid-2">
          <div class="field"><span class="f-label">账号</span>
            <input type="text" id="username" autocomplete="username"></div>
          <div class="field"><span class="f-label">密码</span>
            <input type="password" id="password" autocomplete="current-password"></div>
        </div>
        <div class="field"><span class="f-label">模式</span>
          <span class="row-inline">
            <label class="radio"><input type="radio" name="mode" value="accuracy" checked>刷完成度正确率</label>
            <label class="radio"><input type="radio" name="mode" value="time">刷学习时长</label>
          </span>
          <span class="row-inline">
            <label class="radio"><input type="radio" name="mode" value="queue">顺序刷取</label>
          </span>
        </div>
        <button id="loginBtn" class="btn btn-blue btn-block">登录</button>
      </div>
    </section>

    <section class="panel">
      <div class="p-head"><span class="p-step">2</span><span class="p-title">课程</span></div>
      <div class="p-body">
        <div class="row-inline">
          <select id="courseSel" class="grow"><option value="">-- 请先登录 --</option></select>
          <button id="refreshBtn" class="btn">刷新课程</button>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="p-head"><span class="p-step">3</span><span class="p-title">单元与小节</span></div>
      <div class="p-body">
        <p class="tip" id="unitTip">勾选要刷的单元;勾选单元后展开小节。勾选小节 = 只刷勾选的,不勾 = 刷整个单元。</p>
        <label class="all-row" for="allUnits"><input type="checkbox" id="allUnits">全部单元(忽略下方勾选)</label>
        <div id="unitTree"></div>
      </div>
    </section>

    <section class="panel">
      <div class="p-head"><span class="p-step">4</span><span class="p-title">任务参数</span></div>
      <div class="p-body">
        <div class="field" id="accField">
          <span class="f-label" id="paramLabel">正确率(100 或 70,100):</span>
          <input type="text" id="param">
        </div>
        <div class="field" id="durField" style="display:none">
          <span class="f-label">学习时长秒(30 或 40,90):</span>
          <input type="text" id="paramDur">
        </div>
        <div class="field" id="pauseField" style="display:none">
          <span class="f-label">节间休息秒(3 或 3,12):</span>
          <input type="text" id="paramPause" value="3,12">
        </div>
        <label class="all-row" id="skipDoneRow"
               style="display:none;border:0;padding:0;margin:10px 0 0">
          <input type="checkbox" id="skipDone" checked>跳过已完成章节(不覆盖已有成绩)
        </label>
        <button id="estimateBtn" class="btn btn-block" style="display:none">预估总耗时</button>
      </div>
    </section>
  </div>

  <div class="ops-col">
    <section class="panel ops runbar">
      <div class="run-summary">
        <span class="rs-k">目标</span>
        <span id="runSummary">未选择单元</span>
      </div>
      <div class="run-actions">
        <button id="skipBtn" class="btn btn-lg btn-skip" style="display:none" disabled>跳过当前课程</button>
        <button id="startBtn" class="btn btn-lg btn-run" disabled>开始任务</button>
        <button id="stopBtn" class="btn btn-lg btn-stop" disabled>停止</button>
      </div>
    </section>

    <section class="panel ops console">
      <div class="p-head p-head-d">
        <span class="p-title">运行日志</span>
        <button id="clearBtn" class="btn-ghost-d" type="button">清空</button>
      </div>
      <div id="log"></div>
    </section>
  </div>

</div>
</div>

<script>
'use strict';
var lastSeq = 0;
var courses = [];
var modeSwitching = false;
var lastMode = 'accuracy';
window.__hasTask = false;
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

function setModeLabel() {
  $('paramLabel').textContent = getMode() === 'time'
    ? '每练习时长秒(30 或 10,30):' : '正确率(100 或 70,100):';
}

/* 顺序刷取:任务参数同时出现「正确率」与「学习时长」两个输入框;
   课程与范围仍由「课程」下拉框 + 「单元与小节」勾选决定 */
function applyModeUI() {
  var queue = getMode() === 'queue';
  $('durField').style.display = queue ? '' : 'none';
  $('pauseField').style.display = queue ? '' : 'none';
  $('skipDoneRow').style.display = queue ? 'flex' : 'none';
  $('estimateBtn').style.display = queue ? '' : 'none';
  $('skipBtn').style.display = queue ? '' : 'none';
  $('unitTip').textContent = queue
    ? '勾选要刷的单元与小节:顺序刷取按单元顺序串行刷完勾选范围(勾选小节 = 只刷勾选的,不勾 = 刷整个单元)。'
    : '勾选要刷的单元;勾选单元后展开小节。勾选小节 = 只刷勾选的,不勾 = 刷整个单元。';
  setModeLabel();
  applyAllUnits();
}

function selectMode(mode) {
  var radio = document.querySelector('input[name=mode][value="' + mode + '"]');
  if (radio) radio.checked = true;
  setModeLabel();
  applyModeUI();
}

function setControls(running) {
  var busy = running || modeSwitching;
  $('loginBtn').disabled = busy;
  $('refreshBtn').disabled = busy;
  $('startBtn').disabled = busy || !window.__hasTask;
  $('stopBtn').disabled = !busy;
  $('skipBtn').disabled = !(running && getMode() === 'queue');
  document.querySelectorAll('input[name=mode]').forEach(function (radio) {
    radio.disabled = busy;
  });
  $('statusPill').classList.toggle('running', busy);
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

/* 顺序刷取以「课程」下拉框选中的那门课为准(按单元顺序串行刷完) */
function pickedCids() {
  var cid = $('courseSel').value;
  return cid ? [cid] : [];
}

function selectedCourseName() {
  var cid = $('courseSel').value;
  for (var i = 0; i < courses.length; i++) {
    if (String(courses[i].cid) === cid) return courses[i].name;
  }
  return '';
}

function tag(text, cls) {
  var s = document.createElement('span');
  s.className = 'tag ' + cls;
  s.textContent = text;
  return s;
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
    updateSummary();
    return;
  }
  for (var i = 0; i < list.length; i++) {
    var u = list[i];
    var no = i + 1;
    var visible = String(u.visible || 'true').toLowerCase() === 'true';
    var row = document.createElement('div');
    row.className = 'unit-row';
    var label = document.createElement('label');
    if (!visible) label.className = 'unit-off';   // 未开放单元灰显,仍可勾(其小节会被跳过)
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'unit-cb';
    cb.dataset.no = String(no);
    cb.addEventListener('change', function () { onUnitToggled(this); });
    label.appendChild(cb);
    var noSpan = document.createElement('span');
    noSpan.className = 'u-no';
    noSpan.textContent = 'NO.' + no;
    label.appendChild(noSpan);
    if (!visible) label.appendChild(tag('未开放', 'tag-off'));
    var nameSpan = document.createElement('span');
    nameSpan.className = 'u-name';
    nameSpan.textContent = u.unitname + ' ' + u.name;
    label.appendChild(nameSpan);
    var secBox = document.createElement('div');
    secBox.className = 'sec-box';
    secBox.dataset.no = String(no);
    secBox.style.display = 'none';
    row.appendChild(label);
    row.appendChild(secBox);
    tree.appendChild(row);
  }
  updateSummary();
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
    cb.addEventListener('change', updateSummary);
    label.appendChild(cb);
    var noSpan = document.createElement('span');
    noSpan.className = 'u-no';
    noSpan.textContent = secNo + '.';
    label.appendChild(noSpan);
    label.appendChild(tag(!s.visible ? '未开放' : (s.complete ? '已完成' : '未完成'),
                          !s.visible ? 'tag-off' : (s.complete ? 'tag-done' : 'tag-todo')));
    var nameSpan = document.createElement('span');
    nameSpan.className = 'sec-name';
    nameSpan.textContent = s.location;
    label.appendChild(nameSpan);
    box.appendChild(label);
  }
  updateSummary();
}

/* 勾选单元时懒加载其小节;done 回调用于恢复勾选场景(加载完成后再打勾)。
   加载中重复调用会把 done 入队,由同一次请求返回后统一触发——
   若按"有子节点即已完成"判断,先到的无回调调用会让后到的 done 在
   "加载中"占位符上空跑,勾选恢复静默失效 */
function ensureSecs(no, done) {
  var box = document.querySelector('.sec-box[data-no="' + no + '"]');
  if (!box) { if (done) done(); return; }
  if (box.dataset.loaded === '1') { if (done) done(); return; }
  if (done) {
    if (!box._pendingDones) box._pendingDones = [];
    box._pendingDones.push(done);
  }
  if (box.dataset.loading === '1') return;
  box.dataset.loading = '1';
  var loading = document.createElement('div');
  loading.className = 'dim';
  loading.textContent = '-- 小节加载中... --';
  box.appendChild(loading);
  var flush = function () {
    var q = box._pendingDones || [];
    box._pendingDones = [];
    q.forEach(function (fn) { fn(); });
  };
  apiPost('/api/sections', {unit_index: no})
    .then(function (j) {
      box.dataset.loading = '';
      if (!j.ok) {
        box.innerHTML = '';
        appendLocal('加载小节失败:' + j.error);
      } else {
        renderSecs(no, j.sections);
        applyAllUnits();
        box.dataset.loaded = '1';
      }
      flush();
    })
    .catch(function (err) {
      box.dataset.loading = '';
      box.innerHTML = '';
      appendLocal('请求失败:' + err);
      flush();
    });
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
  updateSummary();
}

function applyAllUnits() {
  var on = $('allUnits').checked;
  document.querySelectorAll('.unit-cb').forEach(function (cb) { cb.disabled = on; });
  document.querySelectorAll('.sec-cb').forEach(function (sc) {
    sc.disabled = on || sc.dataset.baseDisabled === '1';
  });
  $('unitTree').style.opacity = on ? '0.5' : '';
  updateSummary();
}

/* 顺序刷取的运行条摘要:课程名 + 单元/小节范围 + 两组区间参数 */
function queueSelectionSummary() {
  var name = selectedCourseName();
  if (!name) return '请先在课程下拉框中选择课程';
  var scope = '未选择单元';
  var sel = buildSelection();
  if (sel.allUnits) {
    scope = '全部单元';
  } else if (sel.units.length) {
    var secs = 0;
    Object.keys(sel.sections).forEach(function (k) { secs += sel.sections[k].length; });
    scope = sel.units.length + ' 个单元' + (secs ? ' / ' + secs + ' 个小节' : '');
  }
  var acc = $('param').value.trim();
  var dur = $('paramDur').value.trim();
  var parts = [scope];
  if (acc) parts.push('正确率 ' + acc);
  if (dur) parts.push('时长 ' + dur + 's');
  return '顺序刷取 · ' + name + ' · ' + parts.join(' · ');
}

/* 运行条摘要:实时反映勾选目标与参数(纯展示,选择仍以 buildSelection 为准) */
function updateSummary() {
  var el = $('runSummary');
  if (!el) return;
  if (getMode() === 'queue') { el.textContent = queueSelectionSummary(); return; }
  var p = $('param').value.trim();
  var suffix = p ? '(参数 ' + p + ')' : '';
  if ($('allUnits').checked) {
    el.textContent = '全部单元' + suffix;
    return;
  }
  var byNo = {};
  var unitCount = 0;
  document.querySelectorAll('.unit-cb').forEach(function (cb) {
    if (cb.checked) { byNo[cb.dataset.no] = true; unitCount++; }
  });
  if (!unitCount) { el.textContent = '未选择单元'; return; }
  var secCount = 0;
  document.querySelectorAll('.sec-cb:checked').forEach(function (sc) {
    if (!sc.disabled && byNo[sc.dataset.no]) secCount++;
  });
  el.textContent = unitCount + ' 个单元' + (secCount ? ' / ' + secCount + ' 个小节' : '') + suffix;
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
      updateSummary();
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
    var t = document.createElement('span');
    t.className = 't';
    t.textContent = '[' + e[1] + '] ';
    line.appendChild(t);
    line.appendChild(document.createTextNode(e[2]));
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
    var newMode = getMode();
    var previousMode = lastMode;
    lastMode = newMode;
    applyModeUI();
    if (!window.__hasTask) {
      appendLocal('已切换模式,登录后生效');
      return;
    }

    modeSwitching = true;
    setControls(false);
    apiPost('/api/mode', {mode: newMode})
      .then(function (j) {
        if (!j.ok) {
          lastMode = previousMode;
          selectMode(previousMode);
          alert('切换模式失败\n' + j.error);
          modeSwitching = false;
          setControls(false);
          return;
        }
        appendLocal('已切换模式,无需重新登录');
        modeSwitching = false;
        setControls(false);
        updateSummary();
      })
      .catch(function () {
        lastMode = previousMode;
        selectMode(previousMode);
        apiPost('/api/mode', {mode: previousMode})
          .catch(function () {})
          .then(function () {
            modeSwitching = false;
            setControls(false);
          });
      });
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
      applyModeUI();
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
$('param').addEventListener('input', updateSummary);
$('paramDur').addEventListener('input', updateSummary);
$('clearBtn').addEventListener('click', function () { $('log').innerHTML = ''; });

/* 顺序刷取:按「课程」下拉框选中的课程 + 单元/小节勾选生成队列 */
function startQueue() {
  var cids = pickedCids();
  if (!cids.length) { alert('请先在课程下拉框中选择课程'); return; }
  var sel = buildSelection();
  if (!sel.allUnits && !sel.units.length) {
    alert('请勾选要刷的单元,或勾选"全部单元"');
    return;
  }
  var accuracy = $('param').value.trim();
  var duration = $('paramDur').value.trim();
  if (!accuracy || !duration) { alert('顺序刷取需要同时填写正确率与学习时长'); return; }
  apiPost('/api/queue/start', {cids: cids,
                               units: sel.allUnits ? 0 : sel.units,
                               sections: sel.allUnits ? null : sel.sections,
                               accuracy: accuracy, duration: duration,
                               pause: $('paramPause').value,
                               skip_done: $('skipDone').checked})
    .then(function (j) {
      if (!j.ok) { alert(j.error); return; }
      setControls(true);
      $('statusText').textContent = '顺序任务运行中';
      appendLocal('[界面] 顺序任务已开始:共 ' + j.total + ' 门课程,按课程编号顺序执行');
    })
    .catch(function (err) { alert('请求失败:' + err); });
}

$('skipBtn').addEventListener('click', function () {
  apiPost('/api/queue/skip', {})
    .then(function (j) {
      if (!j.ok) { alert(j.error); return; }
      appendLocal('[界面] 已请求跳过当前课程');
    })
    .catch(function (err) { alert('请求失败:' + err); });
});

$('estimateBtn').addEventListener('click', function () {
  var cids = pickedCids();
  if (!cids.length) { alert('请先在课程下拉框中选择课程'); return; }
  if (!$('param').value.trim() || !$('paramDur').value.trim()) {
    alert('请先填写正确率与学习时长');
    return;
  }
  var sel = buildSelection();
  if (!sel.allUnits && !sel.units.length) {
    alert('请勾选要刷的单元,或勾选"全部单元"');
    return;
  }
  $('estimateBtn').disabled = true;
  $('statusText').textContent = '预估耗时中...';
  apiPost('/api/queue/preview', {cids: cids,
                                 units: sel.allUnits ? 0 : sel.units,
                                 sections: sel.allUnits ? null : sel.sections,
                                 accuracy: $('param').value,
                                 duration: $('paramDur').value,
                                 pause: $('paramPause').value})
    .then(function (j) {
      $('estimateBtn').disabled = false;
      $('statusText').textContent = '待命';
      if (!j.ok) { alert('预估失败\n' + j.error); return; }
      appendLocal('[预估] ' + j.rows.length + ' 门课程共 ' + j.sections
                  + ' 个小节,按当前参数预计 ' + j.eta);
      j.rows.forEach(function (r) {
        appendLocal('   NO.' + r.no + ' ' + r.name + ' · '
                    + (r.error ? ('失败:' + r.error) : (r.sections + ' 节')));
      });
      $('runSummary').textContent = '预计 ' + j.eta + '(' + j.sections + ' 节)';
    })
    .catch(function (err) {
      $('estimateBtn').disabled = false;
      alert('请求失败:' + err);
    });
});

$('startBtn').addEventListener('click', function () {
  if (getMode() === 'queue') { startQueue(); return; }
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

/* 运行日志区高度对齐左栏(任务参数底部):日志超出部分在区内滚动,不撑高页面。
   网格行高会被日志内容撑大,纯 CSS 无法约束,故由 JS 把右栏上限钉在左栏自然高度 */
(function () {
  var rail = document.querySelector('.rail');
  var opsCol = document.querySelector('.ops-col');
  var twoCol = window.matchMedia('(min-width: 941px)');   /* 与 CSS 双栏断点(max-width:940px)一致 */
  function syncConsole() {
    if (!twoCol.matches) { opsCol.style.maxHeight = ''; return; }
    opsCol.style.maxHeight = rail.offsetHeight + 'px';
  }
  if (window.ResizeObserver) new ResizeObserver(syncConsole).observe(rail);
  if (twoCol.addEventListener) twoCol.addEventListener('change', syncConsole);
  window.addEventListener('resize', syncConsole);
  syncConsole();
})();

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
      if (j.has_task && j.mode && !modeSwitching && j.mode !== getMode()) {
        lastMode = j.mode;
        selectMode(j.mode);
      }
      if (j.running && j.queue && j.queue.course_pos) {
        $('runSummary').textContent = '第 ' + j.queue.course_pos + '/' + j.queue.course_total
          + ' 门 · 单元 ' + (j.queue.unit_no || 0) + '/' + (j.queue.unit_total || 0)
          + ' · 小节 ' + (j.queue.section_no || 0) + '/' + (j.queue.section_total || 0);
      }
      $('statusText').textContent = j.running_what;
      setControls(j.running);
    })
    .catch(function () { /* 服务未就绪或已关闭,下轮重试 */ });
}

renderTree([]);
applyModeUI();
poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


class _QuietStatusPoll(logging.Filter):
    """日志里不记 /api/status 轮询(每秒一条会刷屏),其余访问行保留"""

    def filter(self, record):
        return "/api/status" not in record.getMessage()


class _PlainAccessFormatter(logging.Formatter):
    """去掉 Werkzeug 写进日志的 ANSI 颜色码,文件里保持纯文本"""

    def __init__(self):
        super().__init__("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record):
        return ANSI_RE.sub("", super().format(record))


def _setup_werkzeug_logging():
    """把 Werkzeug 的启动提示与访问日志改写到 _logs/access.log。

    控制台里那几行彩色文字全部出自 Werkzeug,不是本项目的代码:
      - 红字 "WARNING: This is a development server..."  serving.py log_startup
      - 黄字 "Press CTRL+C to quit"                      serving.py run_simple
      - 黄字 404 / 红字其它 4xx 的请求行                  serving.py log_request
    日志本身不丢,只换出口;控制台只保留本项目自己的输出。
    返回挂上去的 handler,便于测试断言。
    """
    logger = logging.getLogger("werkzeug")
    logger.handlers.clear()
    # 不落回 root:否则 root 没有 handler 时会走 lastResort,WARNING 仍会打到 stderr
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(os.path.join(LOG_DIR, "access.log"),
                                      maxBytes=1_000_000, backupCount=2,
                                      encoding="utf-8")
    except (OSError, ValueError):
        handler = logging.NullHandler()   # 目录不可写也不能因此起不来
    handler.setFormatter(_PlainAccessFormatter())
    logger.addFilter(_QuietStatusPoll())
    logger.addHandler(handler)
    return handler


def main():
    _setup_werkzeug_logging()
    # "* Debug mode: off" / "* Serving Flask app" 两行由 Flask 的横幅函数打印,置空即可
    flask_cli.show_server_banner = lambda *args, **kwargs: None
    port = int(os.environ.get("WELEARN_PORT", "17777"))
    url = f"http://127.0.0.1:{port}/"
    print(BANNER)
    print(f"Web 界面地址: {url}  (Ctrl+C 退出)")
    print(f"访问日志: {os.path.join(LOG_DIR, 'access.log')}")
    if os.environ.get("WELEARN_NO_OPEN") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True)


app, state = create_app()

if __name__ == '__main__':
    main()
