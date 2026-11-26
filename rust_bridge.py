"""Line-delimited JSON bridge used by the StudyPilot Rust helper host.

The automation and session semantics stay in the original Welearn task
classes. This process only translates the local UI protocol.
"""

import json
import sys
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

import welearn_accuracy
import welearn_common
import welearn_study
import welearn_time


class BridgeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.task = None
        self.mode = "accuracy"
        self.courses: List[dict] = []
        self.units: List[dict] = []
        self.sections_map: Dict[int, List[dict]] = {}
        self.running = False
        self.running_what = "待命"
        self.fetching = False
        self.logs = deque(maxlen=2000)
        self.log_seq = 0

    def log(self, message: Any):
        with self.lock:
            self.log_seq += 1
            self.logs.append(
                [self.log_seq, time.strftime("%H:%M:%S"), str(message)]
            )

    def set_state(self, running: bool, running_what: str):
        with self.lock:
            self.running = running
            self.running_what = running_what

    def set_fetching(self, fetching: bool, hint: str = "待命"):
        with self.lock:
            self.fetching = fetching
            self.running_what = hint

    def busy(self) -> bool:
        with self.lock:
            return bool(
                self.running
                or self.fetching
                or (self.worker is not None and self.worker.is_alive())
            )


def _error(state: BridgeState, message: str, code: str = "invalid") -> dict:
    if not state.busy():
        state.log(message)
    return {"ok": False, "error": message, "code": code}


def _mode(value: Any) -> Optional[str]:
    return value if value in ("accuracy", "time", "combined") else None


def _summary(state: BridgeState) -> dict:
    with state.lock:
        return {
            "running": state.running,
            "running_what": state.running_what,
            "fetching": state.fetching,
            "has_task": state.task is not None,
            "mode": state.mode,
        }


def login(state: BridgeState, payload: dict) -> dict:
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    mode = _mode(payload.get("mode", "accuracy"))
    if mode is None:
        return _error(state, "未知模式")
    if not username or not password:
        return _error(state, "请输入账号和密码")
    if state.busy():
        return _error(state, "任务正在进行，请先停止", "busy")

    state.stop_event.clear()
    state.set_fetching(True, "登录中...")
    task_class = (
        welearn_accuracy.AccuracyTask
        if mode == "accuracy"
        else welearn_time.TimeTask
    )
    task = task_class(
        username, password, on_log=state.log, stop_event=state.stop_event
    )
    try:
        task.login()
        courses = task.fetch_courses()
    except (welearn_common.LoginError, welearn_common.WelearnError) as error:
        return _error(state, str(error), "login")
    except Exception as error:
        return _error(state, repr(error), "login")
    finally:
        state.set_fetching(False)

    with state.lock:
        state.task = task
        state.mode = mode
        state.courses = courses
        state.units = []
        state.sections_map = {}
    state.log(f"共 {len(courses)} 门课程，请在课程列表中选择")
    return {"ok": True, "courses": courses, **_summary(state)}


def refresh(state: BridgeState, _payload: dict) -> dict:
    if state.task is None:
        return _error(state, "请先登录")
    if state.busy():
        return _error(state, "任务正在进行，请先停止", "busy")
    state.set_fetching(True, "刷新课程中...")
    try:
        courses = state.task.fetch_courses()
    except (welearn_common.LoginError, welearn_common.WelearnError) as error:
        return _error(state, str(error), "refresh")
    except Exception as error:
        return _error(state, repr(error), "refresh")
    finally:
        state.set_fetching(False)
    with state.lock:
        state.courses = courses
    return {"ok": True, "courses": courses, **_summary(state)}


def logout(state: BridgeState, _payload: dict) -> dict:
    if state.busy():
        return _error(state, "任务正在进行，请先停止", "busy")
    with state.lock:
        state.task = None
        state.courses = []
        state.units = []
        state.sections_map = {}
    state.log("已退出登录，请重新登录")
    return {"ok": True, **_summary(state)}


def units(state: BridgeState, payload: dict) -> dict:
    if state.task is None:
        return _error(state, "请先登录")
    if state.busy():
        return _error(state, "任务正在进行，请先停止", "busy")
    try:
        cid = int(payload.get("cid"))
    except (TypeError, ValueError):
        return _error(state, "课程编号必须是数字")
    state.set_fetching(True, "加载单元中...")
    try:
        loaded = state.task.fetch_units(cid)
    except (welearn_common.LoginError, welearn_common.WelearnError) as error:
        return _error(state, str(error), "units")
    except Exception as error:
        return _error(state, f"{error!r}", "units")
    finally:
        state.set_fetching(False)
    with state.lock:
        state.units = loaded
    state.log(f"已加载 {len(loaded)} 个单元")
    return {"ok": True, "units": loaded, **_summary(state)}


def sections(state: BridgeState, payload: dict) -> dict:
    if state.task is None:
        return _error(state, "请先登录")
    if not state.units:
        return _error(state, "请先选择课程加载单元")
    if state.busy():
        return _error(state, "任务正在进行，请先停止", "busy")
    try:
        unit_index = int(payload.get("unit_index"))
    except (TypeError, ValueError):
        return _error(state, "单元序号必须是数字")
    if not 1 <= unit_index <= len(state.units):
        return _error(state, f"请选择具体单元(1~{len(state.units)})")
    state.set_fetching(True, "加载小节中...")
    try:
        raw = state.task.fetch_sections(unit_index)
    except (welearn_common.LoginError, welearn_common.WelearnError) as error:
        return _error(state, str(error), "sections")
    except Exception as error:
        return _error(state, repr(error), "sections")
    finally:
        state.set_fetching(False)
    sections_list = [
        {
            "location": item.get("location", ""),
            "complete": "未" not in item.get("iscomplete", "未"),
            "visible": item.get("isvisible", "true") != "false",
        }
        for item in raw
    ]
    with state.lock:
        state.sections_map[unit_index] = sections_list
    state.log(f"单元 {unit_index} 共 {len(sections_list)} 个小节")
    return {"ok": True, "sections": sections_list, **_summary(state)}


def mode(state: BridgeState, payload: dict) -> dict:
    mode_name = _mode(payload.get("mode"))
    if mode_name is None:
        return _error(state, "未知模式")
    with state.lock:
        if state.mode == mode_name:
            return {"ok": True, "mode": mode_name, **_summary(state)}
        if state.task is None:
            state.mode = mode_name
            return {"ok": True, "mode": mode_name, **_summary(state)}
        if state.running or state.fetching:
            return _error(state, "任务正在进行，请先停止", "busy")
        old_task = state.task
        task_class = (
            welearn_accuracy.AccuracyTask
            if mode_name == "accuracy"
            else welearn_time.TimeTask
        )
        task = task_class(
            old_task.username,
            old_task.password,
            on_log=state.log,
            stop_event=state.stop_event,
            session=old_task.session,
        )
        for name in ("cid", "uid", "classid"):
            setattr(task, name, getattr(old_task, name, ""))
        task.units = list(getattr(old_task, "units", []) or [])
        state.task = task
        state.mode = mode_name
    label = {
        "accuracy": "刷完成度正确率",
        "time": "刷学习时长",
        "combined": "时长与正确率",
    }[mode_name]
    state.log(f"已切换到{label}模式，无需重新登录")
    return {"ok": True, "mode": mode_name, **_summary(state)}


def start(state: BridgeState, payload: dict) -> dict:
    if state.task is None:
        return _error(state, "请先登录")
    if not state.units:
        return _error(state, "请先选择课程加载单元")
    raw_units = payload.get("units", 0)
    raw_sections = payload.get("sections")
    run_mode = state.mode
    has_unified_fields = "duration" in payload or "accuracy" in payload
    try:
        all_units, unit_list, section_map = welearn_common.normalize_targets(
            raw_units, raw_sections
        )
        if run_mode == "combined" or has_unified_fields:
            raw_duration = payload.get("duration", payload.get("param"))
            raw_accuracy = payload.get("accuracy")
            duration_text = str(
                raw_duration if raw_duration is not None else ""
            ).strip()
            accuracy_text = str(
                raw_accuracy if raw_accuracy is not None else ""
            ).strip()
            parameter = (
                welearn_common.parse_numeric_param(duration_text)
                if duration_text else None
            )
            accuracy = (
                welearn_common.parse_numeric_param(accuracy_text)
                if accuracy_text else None
            )
            parallel_workers = int(payload.get("parallel_workers", 1))
            if not 1 <= parallel_workers <= welearn_study.MAX_PARALLEL_WORKERS:
                raise ValueError(
                    f"并发小节数必须在 1~{welearn_study.MAX_PARALLEL_WORKERS} 之间"
                )
        else:
            parameter = welearn_common.parse_numeric_param(
                str(payload.get("param", ""))
            )
            accuracy = None
            parallel_workers = 1
    except (TypeError, ValueError) as error:
        return _error(state, str(error))
    with state.lock:
        known_sections = dict(state.sections_map)
        if state.running or state.fetching or (
            state.worker is not None and state.worker.is_alive()
        ):
            return _error(state, "任务正在进行，请先停止", "busy")
        if section_map:
            for unit_no, selected in section_map.items():
                known = known_sections.get(unit_no)
                if known and max(selected) > len(known):
                    return _error(
                        state,
                        f"单元{unit_no}小节编号超出范围(1~{len(known)})",
                    )
        state.stop_event.clear()
        state.running = True
        state.running_what = "任务运行中"
        task = state.task

        def work():
            try:
                result = task.run(
                    0 if all_units else unit_list,
                    parameter,
                    section_map,
                    **({"accuracy": accuracy}
                       if run_mode == "combined" or has_unified_fields
                       else {}),
                    **({"parallel_workers": parallel_workers}
                       if run_mode == "combined" or has_unified_fields
                       else {}),
                )
                state.log(_format_result(run_mode, result))
            except Exception as error:
                state.log(f"任务异常终止：{error!r}")
            finally:
                state.set_state(False, "待命")

        state.worker = threading.Thread(target=work, daemon=True)
        worker = state.worker
    target = "全部单元" if all_units else "单元" + ",".join(
        str(unit) for unit in unit_list or []
    )
    if section_map:
        target += " 小节" + ",".join(
            f"{unit}:[{'/'.join(str(section) for section in selected)}]"
            for unit, selected in section_map.items()
        )
    parameter_text = f"参数={parameter}"
    if run_mode == "combined" or has_unified_fields:
        parameter_text = (
            f"时长={parameter if parameter is not None else '20,40'}"
            f" 正确率={accuracy if accuracy is not None else '保留原值'}"
            f" 并发={parallel_workers}"
        )
    state.log(f"开始任务：{target} {parameter_text}")
    worker.start()
    return {"ok": True, **_summary(state)}


def _format_result(mode_name: str, result: dict) -> str:
    if (result.get("accuracy_submitted")
            or (mode_name == "accuracy" and "way1_succeed" in result)):
        lines = [
            f"方式1: {result.get('way1_succeed', 0)} 成功, "
            f"{result.get('way1_failed', 0)} 失败",
            f"方式2: {result.get('way2_succeed', 0)} 成功, "
            f"{result.get('way2_failed', 0)} 失败",
        ]
        if "succeed" in result:
            lines.append(
                f"学习记录: {result.get('succeed', 0)} 成功, "
                f"{result.get('failed', 0)} 失败"
            )
        return "\n".join(lines)
    if mode_name in ("time", "combined"):
        failures = result.get("errors", [])
        lines = [f"运行结束!!错误: {result.get('failed', len(failures))} 个"]
        lines.extend(
            f"第{index}个错误:{error}"
            for index, error in enumerate(failures, start=1)
        )
        return "\n".join(lines)
    return (
        f"方式1: {result.get('way1_succeed', 0)} 成功, "
        f"{result.get('way1_failed', 0)} 失败\n"
        f"方式2: {result.get('way2_succeed', 0)} 成功, "
        f"{result.get('way2_failed', 0)} 失败"
    )


def stop(state: BridgeState, _payload: dict) -> dict:
    state.stop_event.set()
    state.log("已请求停止，等待任务收尾...")
    return {"ok": True, **_summary(state)}


def status(state: BridgeState, payload: dict) -> dict:
    try:
        after = int(payload.get("after", 0))
    except (TypeError, ValueError):
        after = 0
    with state.lock:
        logs = [entry for entry in state.logs if entry[0] > after]
    return {"ok": True, "logs": logs, **_summary(state)}


ACTIONS = {
    "login": login,
    "refresh": refresh,
    "logout": logout,
    "units": units,
    "sections": sections,
    "mode": mode,
    "start": start,
    "stop": stop,
    "status": status,
}


def main():
    state = BridgeState()
    for line in sys.stdin:
        try:
            request = json.loads(line.lstrip("\ufeff"))
            action = request.get("action")
            payload = request.get("payload") or {}
            if action not in ACTIONS:
                response = {"ok": False, "error": f"未知操作: {action}"}
            else:
                response = ACTIONS[action](state, payload)
        except Exception as error:
            response = {"ok": False, "error": repr(error)}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
