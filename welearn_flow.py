"""顺序任务:多门课程按课程编号串行,每个小节一次运行同时完成「模拟学习时长」与「写入正确率」。

与 AccuracyTask / TimeTask 的三点不同:

1. 串行 —— 同一时刻只有一个小节在计时(不用 asyncio.gather 并发),更像真实学习行为
2. 合并 —— 一次遍历同时写时长与正确率,避免两轮请求与「已完成但时长为 0」的中间态
3. 队列 —— 按课程编号升序串起多门课程;stop_event 停整个队列,skip_event 只跳当前课程
"""
import json
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import welearn_common
from welearn_accuracy import SCO_DATA_TEMPLATE
from welearn_common import LoginError, WelearnError

AJAX_URL = "https://welearn.sflep.com/Ajax/SCO.aspx"
KEEPALIVE_EVERY = 60          # 与 TimeTask 一致:每 60 秒补一次心跳
HEARTBEAT_INTERVAL = 10       # 与 TimeTask 一致:每 10 秒汇报一次进度
DEFAULT_REQUEST_INTERVAL = 2.0
DEFAULT_PAUSE = (3, 12)       # 小节之间休息,避免请求过于密集


class _Stopped(Exception):
    """内部信号:用户点了停止,用于从任意等待点跳出"""


def _default_log(msg):
    print(msg)


def to_span(value) -> Union[int, Tuple[int, int]]:
    """'70,95' / [70, 95] / 70 → 区间或定值;非法输入抛 ValueError。"""
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ValueError(f"区间需要两个数:{value!r}")
        lo, hi = int(value[0]), int(value[1])
        if lo > hi:
            raise ValueError(f"区间下限大于上限({lo} > {hi})")
        return lo, hi
    if isinstance(value, bool):
        raise ValueError("不支持布尔值")
    if isinstance(value, int):
        return value
    return welearn_common.parse_numeric_param(str(value))


def sample_span(span: Union[int, Tuple[int, int]]) -> int:
    """按区间随机取样;定值原样返回。"""
    if isinstance(span, tuple):
        return random.randint(span[0], span[1])
    return int(span)


def avg_of(span: Union[int, Tuple[int, int]]) -> float:
    """区间中点,用于耗时预估。"""
    if isinstance(span, tuple):
        return (span[0] + span[1]) / 2.0
    return float(span)


def normalize_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """校验并补全一条队列项(浏览器 JSON 直接传来,不可信任)。非法抛 ValueError。"""
    cid = str(job.get("cid", "")).strip()
    if not cid:
        raise ValueError("课程 cid 不能为空")
    try:
        no = int(job.get("no", 0))
    except (TypeError, ValueError):
        raise ValueError("课程编号必须是数字")
    name = str(job.get("name", "") or f"课程{no}")

    try:
        accuracy = to_span(job.get("accuracy"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"NO.{no} 正确率格式错误:{e}")
    try:
        duration = to_span(job.get("duration"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"NO.{no} 学习时长格式错误:{e}")
    pause = to_span(job.get("pause") if job.get("pause") is not None else DEFAULT_PAUSE)

    def bounds(span):
        return span if isinstance(span, tuple) else (span, span)

    lo, hi = bounds(accuracy)
    if lo < 0:
        raise ValueError(f"NO.{no} 正确率不能为负数")
    if hi > 100:
        raise ValueError(f"NO.{no} 正确率不能超过 100")
    lo, hi = bounds(duration)
    if lo < 0:
        raise ValueError(f"NO.{no} 学习时长不能为负数")

    try:
        all_units, unit_list, section_map = welearn_common.normalize_targets(
            job.get("units", 0), job.get("sections") or None)
    except ValueError as e:
        raise ValueError(f"NO.{no} 刷取范围错误:{e}")

    return {"no": no, "cid": cid, "name": name, "accuracy": accuracy,
            "duration": duration, "pause": pause,
            "units": 0 if all_units else unit_list,
            "sections": section_map or None,
            "skip_done": bool(job.get("skip_done", True))}


def _hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


class FlowTask:
    """顺序任务控制器。接口与 AccuracyTask / TimeTask 同构,便于 /api/mode 复用会话切换。"""

    def __init__(self, username: str, password: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None,
                 session: Optional[Any] = None,
                 skip_event: Optional[threading.Event] = None,
                 request_interval: float = DEFAULT_REQUEST_INTERVAL):
        self.username = username
        self.password = password
        self.on_log = on_log or _default_log
        self.stop_event = stop_event or threading.Event()
        self.skip_event = skip_event or threading.Event()
        self.session = session or welearn_common.new_session()
        self.request_interval = request_interval
        self.cid = ''
        self.uid = ''
        self.classid = ''
        self.units: List[Any] = []
        self.progress: Dict[str, Any] = {}   # 供 /api/status 读取,只写不读
        # 心跳统计:每 10 秒汇报「本小节已刷 / 本课程累计 / 本次任务累计」
        self.queue_started_at = 0.0
        self.course_learned = 0              # 本课程已完成的秒数
        self._last_beat = 0.0

    # ——————————————————— 基础 ———————————————————

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def _wait(self, seconds: float) -> None:
        """可中断等待:停止事件一置位立刻抛 _Stopped。"""
        if self.stop_event.wait(max(0.0, float(seconds))):
            raise _Stopped()

    @staticmethod
    def _mmss(seconds: float) -> str:
        seconds = int(max(0, seconds))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _beat(self, done: int, target: int) -> None:
        """每 10 秒汇报一次:本小节进度 / 本课程累计学习 / 本次任务累计用时"""
        now = time.time()
        if self._last_beat and now - self._last_beat < HEARTBEAT_INTERVAL:
            return
        self._last_beat = now
        elapsed = now - self.queue_started_at if self.queue_started_at else 0
        self.on_log(f"[心跳] 本小节 {done}/{target} 秒 · 本课程已学 "
                    f"{self._mmss(self.course_learned + done)} · 本次任务累计 "
                    f"{self._mmss(elapsed)}")

    def login(self) -> None:
        welearn_common.login(self.session, self.username, self.password,
                             on_log=self.on_log, stop_event=self.stop_event)

    def fetch_courses(self) -> List[dict]:
        return welearn_common.fetch_courses(self.session)

    def fetch_units(self, cid) -> List[dict]:
        self.cid = str(cid)
        # 与原实现一致:先请求 /2019/ 页面(响应弃用,保持请求序列)
        self.session.get(
            'https://welearn.sflep.com/2019/student/course_info.aspx?cid=' + self.cid,
            headers={'Referer': 'https://welearn.sflep.com/2019/student/index.aspx'})
        self.uid, self.classid = welearn_common.get_course_ids(self.session, self.cid)
        self.units = welearn_common.fetch_units(self.session, self.cid, self.uid)
        return self.units

    def fetch_sections(self, unit_index: int) -> List[dict]:
        """只读获取指定单元(1-based)的小节列表"""
        return welearn_common.fetch_sections(self.session, self.cid, self.uid,
                                             unit_index - 1, self.classid)

    # ——————————————————— 协议细节 ———————————————————

    @staticmethod
    def read_cmi(response) -> Dict[str, str]:
        """从 getscoinfo_v7 响应取出 cmi;任何异常/缺字段都回退默认值(不抛异常)。"""
        cmi = None
        try:
            comment = response.json().get("comment")
            payload = json.loads(comment) if isinstance(comment, str) and comment else comment
            if isinstance(payload, dict):
                cmi = payload.get("cmi")
        except Exception:
            cmi = None
        if not isinstance(cmi, dict):
            return {"crate": "", "cstatus": "not_attempted", "progress": "0",
                    "total_time": "0", "session_time": "0"}
        score = cmi.get("score") if isinstance(cmi.get("score"), dict) else {}
        return {"crate": score.get("scaled", ""),
                "cstatus": cmi.get("completion_status", "unknown"),
                "progress": cmi.get("progress_measure", "0"),
                "total_time": cmi.get("total_time", "0"),
                "session_time": cmi.get("session_time", "0")}

    def _keepalive(self, common: dict, headers: dict,
                   session_time: str, total_time: str) -> None:
        self.session.post(
            AJAX_URL,
            data={**common, 'action': 'keepsco_with_getticket_with_updatecmitime',
                  'session_time': session_time, 'total_time': total_time},
            headers=headers)

    def _learn_section(self, section: Dict[str, Any], seconds: int, accuracy: int) -> None:
        """一个小节:读取已学 → 心跳等待 seconds 秒 → 写入时长与正确率(一次提交)"""
        sco = section["id"]
        common = {'uid': self.uid, 'cid': self.cid, 'scoid': sco}
        headers = {'Referer': f"https://welearn.sflep.com/student/StudyCourse.aspx"
                              f"?cid={self.cid}&classid={self.classid}&sco={sco}"}

        resp = self.session.post(AJAX_URL, data={**common, 'action': 'getscoinfo_v7'},
                                 headers=headers)
        if '学习数据不正确' in resp.text:
            self._wait(self.request_interval)
            self.session.post(AJAX_URL, data={**common, 'action': 'startsco160928'},
                              headers=headers)
            resp = self.session.post(AJAX_URL, data={**common, 'action': 'getscoinfo_v7'},
                                     headers=headers)
            if '学习数据不正确' in resp.text:
                raise WelearnError(f"学习数据不正确:{section.get('location', sco)}")

        cmi = self.read_cmi(resp)
        session_time, total_time = cmi["session_time"], cmi["total_time"]

        self._wait(self.request_interval)
        self._keepalive(common, headers, session_time, total_time)

        for tick in range(1, seconds + 1):
            self._wait(1)
            if tick % KEEPALIVE_EVERY == 0:
                self._keepalive(common, headers, session_time, total_time)
            self._beat(tick, seconds)

        # ——— 写入正确率:与 AccuracyTask 完全相同的 方式1+方式2 ———
        # 只调 savescoinfo160928(方式2)时服务端不会落正确率,必须先用
        # startsco160928 + setscoinfo 把 score 写进 cmi,再 savescoinfo 收尾。
        # 唯一的差异:模板里的 session_time/total_time 用"最新"读数回填,
        # 否则模板中硬编码的 "0" 会把刚刷出来的时长清零。
        self._wait(self.request_interval)
        try:
            fresh = self.read_cmi(self.session.post(
                AJAX_URL, data={**common, "action": "getscoinfo_v7"}, headers=headers))
        except Exception:
            fresh = {}
        total_time = fresh.get("total_time") or total_time
        session_time = fresh.get("session_time") or session_time

        self.session.post(AJAX_URL, data={**common, "action": "startsco160928"},
                          headers=headers)
        data = (SCO_DATA_TEMPLATE
                .replace('{crate}', str(accuracy))
                .replace('"session_time":"0"', f'"session_time":"{session_time}"')
                .replace('"total_time":"0"', f'"total_time":"{total_time}"'))
        self.session.post(AJAX_URL,
                          data={**common, 'action': 'setscoinfo', 'data': data,
                                'isend': 'False'},
                          headers=headers)
        self.session.post(AJAX_URL,
                          data={**common, 'action': 'savescoinfo160928',
                                'progress': '100', 'crate': str(accuracy),
                                'status': 'unknown', 'cstatus': 'completed',
                                'trycount': '0'},
                          headers=headers)

    # ——————————————————— 队列执行 ———————————————————

    def run_course(self, job: Dict[str, Any], course_pos: int = 1,
                   course_total: int = 1) -> Dict[str, Any]:
        """执行单门课程(阻塞)。停止/异常都折算进返回值,不向上抛,便于队列继续。"""
        job = normalize_job(job)
        stats = {"no": job["no"], "cid": job["cid"], "name": job["name"],
                 "status": "done", "total": 0, "done": 0, "skipped": 0,
                 "failed": 0, "errors": [], "learned_seconds": 0, "elapsed": 0.0}
        self.course_learned = 0
        if not self.queue_started_at:
            self.queue_started_at = time.time()
            self._last_beat = self.queue_started_at
        started = time.time()
        try:
            self._run_course_inner(job, stats, course_pos, course_total)
        except _Stopped:
            stats["status"] = "stopped"
            self.on_log(f"[课程 {course_pos}/{course_total}] 已停止,保留已完成的部分")
        except (LoginError, WelearnError) as e:
            stats["status"] = "failed"
            stats["errors"].append(str(e))
            self.on_log(f"[课程 {course_pos}/{course_total}] 中止:{e}")
        except Exception as e:
            stats["status"] = "failed"
            stats["errors"].append(repr(e))
            self.on_log(f"[课程 {course_pos}/{course_total}] 异常:{e!r}")
        stats["elapsed"] = round(time.time() - started, 1)
        return stats

    def _run_course_inner(self, job: dict, stats: dict,
                          course_pos: int, course_total: int) -> None:
        units = self.fetch_units(job["cid"])
        all_units, unit_list, section_map = welearn_common.normalize_targets(
            job.get("units", 0), job.get("sections"))
        if all_units:
            targets = list(range(len(units)))
        else:
            targets = [uno - 1 for uno in unit_list]
            overflow = [uno for uno in unit_list if uno > len(units)]
            if overflow:
                self.on_log(f"[课程 {course_pos}/{course_total}] 单元 "
                            f"{','.join(str(u) for u in overflow)} 超出该课程范围"
                            f"(共 {len(units)} 个单元),已忽略")
            targets = [index for index in targets if 0 <= index < len(units)]
            if not targets:
                stats["status"] = "skipped"
                self.on_log(f"[课程 {course_pos}/{course_total}] 没有可执行的单元,跳过该课程")
                return

        unit_total = len(targets)
        self.progress.update({"course_pos": course_pos, "course_total": course_total,
                              "course_no": job["no"], "course_name": job["name"],
                              "unit_no": 0, "unit_total": unit_total,
                              "section_no": 0, "section_total": 0})
        scope = "全部单元" if all_units else f"指定 {unit_total} 个单元"
        self.on_log(f"[课程 {course_pos}/{course_total}] NO.{job['no']} {job['name']}"
                    f" · {scope}(共 {len(units)} 个单元)")

        for position, unit_index in enumerate(targets, start=1):
            if self._stopped():
                raise _Stopped()
            if self.skip_event.is_set():
                stats["status"] = "skipped"
                self.on_log(f"[课程 {course_pos}/{course_total}] 已跳过剩余单元")
                return
            unit = units[unit_index]
            if str(unit.get("visible", "true")).lower() == "false":
                self.on_log(f"[单元 {position}/{unit_total}] 未开放,跳过 "
                            f"{unit.get('unitname', '')}{unit.get('name', '')}")
                continue

            self.progress.update({"unit_no": position})
            response = self.session.get(
                f"https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"
                f"&cid={self.cid}&uid={self.uid}&unitidx={unit_index}&classid={self.classid}",
                headers={'Referer': f"https://welearn.sflep.com/2019/student/"
                                    f"course_info.aspx?cid={self.cid}"})
            if "异常" in response.text or "出错了" in response.text:
                self.on_log(f"[单元 {position}/{unit_total}] 无小节数据,跳过")
                continue
            try:
                sections = response.json()["info"]
            except Exception:
                self.on_log(f"[单元 {position}/{unit_total}] 小节数据解析失败,跳过")
                continue

            self.progress.update({"section_total": len(sections)})
            wanted = section_map.get(unit_index + 1) if section_map else None
            self._run_sections(job, sections, stats, course_pos, course_total,
                               unit_total, wanted)

    def _run_sections(self, job: dict, sections: List[dict], stats: dict,
                      course_pos: int, course_total: int, unit_total: int,
                      wanted: Optional[List[int]] = None) -> None:
        unit_no = self.progress.get("unit_no", 0)
        selected = [no for no in range(1, len(sections) + 1)
                    if not wanted or no in wanted]
        total_sections = len(selected)
        for position, no in enumerate(selected, start=1):
            section = sections[no - 1]
            if self._stopped():
                raise _Stopped()
            if self.skip_event.is_set():
                stats["status"] = "skipped"
                self.on_log(f"[课程 {course_pos}/{course_total}] 已跳过剩余小节")
                return
            self.progress.update({"section_no": position})
            location = section.get("location", f"小节{no}")
            prefix = (f"[课程 {course_pos}/{course_total}]"
                      f"[单元 {unit_no}/{unit_total}][小节 {position}/{total_sections}]")

            if not welearn_common.is_visible(section):
                self.on_log(f"{prefix} 未开放,跳过 {location}")
                stats["skipped"] += 1
                continue
            if job["skip_done"] and not welearn_common.is_unfinished(section):
                self.on_log(f"{prefix} 已完成,跳过 {location}")
                stats["skipped"] += 1
                continue

            seconds = sample_span(job["duration"])
            accuracy = sample_span(job["accuracy"])
            stats["total"] += 1
            self.on_log(f"{prefix} 模拟学习 {seconds}s · 正确率 {accuracy}%  {location}")
            try:
                self._learn_section(section, seconds, accuracy)
            except _Stopped:
                raise
            except (LoginError, WelearnError) as e:
                stats["failed"] += 1
                stats["errors"].append(f"{location}: {e}")
                self.on_log(f"{prefix} 失败 {location}:{e}")
                continue
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append(f"{location}: {e!r}")
                self.on_log(f"{prefix} 异常 {location}:{e!r}")
                continue

            stats["done"] += 1
            stats["learned_seconds"] += seconds
            self.course_learned += seconds
            self.on_log(f"{prefix} 完成 {location} · 本课程累计学习 "
                        f"{self._mmss(self.course_learned)}")

            pause = sample_span(job["pause"])
            if pause > 0:
                self._wait(pause)

    def run_queue(self, jobs: List[Dict[str, Any]], on_error: str = "continue") -> Dict[str, Any]:
        """按课程编号升序串行执行全部队列项。"""
        jobs = sorted((normalize_job(job) for job in jobs), key=lambda j: j["no"])
        started = time.time()
        self.queue_started_at = started
        self._last_beat = started
        courses: List[dict] = []
        for position, job in enumerate(jobs, start=1):
            if self._stopped():
                break
            self.skip_event.clear()
            result = self.run_course(job, position, len(jobs))
            courses.append(result)
            if result["status"] == "stopped" and self._stopped():
                break
            if result["status"] == "failed" and on_error == "halt":
                self.on_log("配置为失败即停,队列终止")
                break

        summary = {"total": len(jobs), "courses": courses,
                   "done": sum(c["done"] for c in courses),
                   "skipped": sum(c["skipped"] for c in courses),
                   "failed": sum(c["failed"] for c in courses),
                   "elapsed": round(time.time() - started, 1),
                   "stopped": self._stopped()}
        self.progress.update({"finished": True})
        return summary


def welearn_flow_run():
    """命令行入口:选多门课程 → 两组参数 → 按课程编号顺序串行执行"""
    from textwrap import dedent

    user = input("请输入用户名=>")
    pwd = input("请输入密码=>")
    task = FlowTask(user, pwd)
    try:
        task.login()
    except (LoginError, WelearnError) as e:
        print(e)
        return
    try:
        courses = task.fetch_courses()
    except WelearnError as e:
        print(e)
        return

    for index, course in enumerate(courses, start=1):
        print(f"[NO.{index:>2d}] 完成度{course['per']:>3}% {course['name']}")
    raw = input("\n请输入要顺序刷取的课程编号(英文逗号分隔,如 1,3,4): ")
    try:
        numbers = sorted({int(x) for x in raw.replace(' ', '').split(',') if x})
    except ValueError:
        print("课程编号格式异常")
        return
    picked = [c for i, c in enumerate(courses, start=1) if i in numbers]
    if not picked:
        print("未选中任何课程")
        return

    print(dedent('''\
        正确率:定值 90 或区间 70,95
        学习时长(秒):定值 60 或区间 40,90
    '''))
    try:
        accuracy = to_span(input("每个小节正确率=>"))
        duration = to_span(input("每个小节学习时长(秒)=>"))
    except ValueError as e:
        print(f"参数格式异常:{e}")
        return

    jobs = [{"no": i + 1, "cid": c["cid"], "name": c["name"],
             "accuracy": accuracy, "duration": duration, "skip_done": True}
            for i, c in enumerate(courses) if (i + 1) in numbers]
    est = sum(avg_of(j["duration"]) + avg_of(j["pause"]) for j in jobs)
    print(f"\n共 {len(jobs)} 门课程,按课程编号顺序执行;"
          f"若每门课按 30 个小节估算,总耗时约 {_hms(est * 30)}")
    if input("确认开始?(y/N) ").strip().lower() != 'y':
        return

    result = task.run_queue(jobs)
    print(f"\n顺序任务结束:完成 {result['done']} 节 / 跳过 {result['skipped']} 节 "
          f"/ 失败 {result['failed']} 节 / 用时 {_hms(result['elapsed'])}")
    input("Press Enter to exit...")
