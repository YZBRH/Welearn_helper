import json
import threading
from concurrent.futures import ThreadPoolExecutor
from random import randint
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import welearn_common
from welearn_common import WelearnError

AJAX_URL = "https://welearn.sflep.com/Ajax/SCO.aspx"

SCO_DATA_TEMPLATE = (
    '{"cmi":{"completion_status":"completed","interactions":[],"launch_data":"",'
    '"progress_measure":"1","score":{"scaled":"{crate}","raw":"100"},'
    '"session_time":"{session_time}","success_status":"unknown",'
    '"total_time":"{total_time}","mode":"normal"},'
    '"adl":{"data":[]},"cci":{"data":[],"service":{"dictionary":{"headword":"",'
    '"short_cuts":""},"new_words":[],"notes":[],"writing_marking":[],"record":'
    '{"files":[]},"play":{"offline_media_id":"9999"}},"retry_count":"0",'
    '"submit_time":""}}[INTERACTIONINFO]'
)

DEFAULT_STUDY_SECONDS = (20, 40)
DEFAULT_SECTION_GAP_SECONDS = (4, 10)
KEEPALIVE_EVERY = 60
MAX_PARALLEL_WORKERS = 4

NumericSpan = Union[int, Tuple[int, int]]


def _default_log(message: str) -> None:
    print(message)


class StudyTask:
    """统一的心跳、学习时长与成绩提交内核。"""

    def __init__(self, username: str, password: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None,
                 session: Optional[Any] = None,
                 request_interval: float = 0.5):
        self.username = username
        self.password = password
        self.on_log = on_log or _default_log
        self.stop_event = stop_event or threading.Event()
        self.session = session or welearn_common.new_session()
        self._request_lock = threading.RLock()
        self.request_interval = request_interval
        self.cid = ""
        self.uid = ""
        self.classid = ""
        self.units: List[Any] = []

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def _wait(self, seconds: float) -> bool:
        return not self.stop_event.wait(max(0.0, float(seconds)))

    def _get(self, url: str, **kwargs):
        with self._request_lock:
            return self.session.get(url, **kwargs)

    def _post(self, url: str, **kwargs):
        with self._request_lock:
            return self.session.post(url, **kwargs)

    @staticmethod
    def _sample_span(span: NumericSpan) -> int:
        if isinstance(span, int):
            return span
        return randint(int(span[0]), int(span[1]))

    @staticmethod
    def _is_complete(course: Dict[str, Any]) -> bool:
        status = str(course.get("iscomplete", "")).strip()
        return "未" not in status and (
            "已" in status or status.lower() in {"completed", "complete", "true"}
        )

    @staticmethod
    def _read_cmi(response) -> Dict[str, str]:
        defaults = {
            "session_time": "0",
            "total_time": "0",
            "crate": "",
            "cstatus": "not_attempted",
            "progress": "0",
        }
        try:
            payload = response.json()
            comment = payload.get("comment", "")
            parsed = json.loads(comment) if isinstance(comment, str) and comment else comment
            cmi = parsed.get("cmi", {}) if isinstance(parsed, dict) else {}
            if not isinstance(cmi, dict):
                return defaults
            score = cmi.get("score")
            if not isinstance(score, dict):
                score = {}
            return {
                "session_time": str(cmi.get("session_time") or "0"),
                "total_time": str(cmi.get("total_time") or "0"),
                "crate": str(score.get("scaled") or ""),
                "cstatus": str(cmi.get("completion_status") or "not_attempted"),
                "progress": str(cmi.get("progress_measure") or "0"),
            }
        except Exception:
            return defaults

    @staticmethod
    def _read_elapsed_seconds(response) -> int:
        try:
            return max(0, int(response.json().get("seconds", 0)))
        except Exception:
            return 0

    @staticmethod
    def _response_ok(response) -> bool:
        if '"ret":0' in response.text:
            return True
        try:
            payload = response.json()
            return "ret" not in payload or payload.get("ret") == 0
        except Exception:
            return False

    @staticmethod
    def _add_seconds(time_value: str, seconds: int) -> str:
        raw = str(time_value or "0")
        try:
            if ":" in raw:
                hours, minutes, secs = (int(float(part)) for part in raw.split(":"))
            else:
                hours, minutes, secs = 0, 0, int(float(raw))
        except (TypeError, ValueError):
            hours, minutes, secs = 0, 0, 0
        total = max(0, hours * 3600 + minutes * 60 + secs + int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def login(self) -> None:
        welearn_common.login(
            self.session,
            self.username,
            self.password,
            on_log=self.on_log,
            stop_event=self.stop_event,
        )

    def fetch_courses(self) -> List[dict]:
        return welearn_common.fetch_courses(self.session)

    def fetch_units(self, cid) -> List[dict]:
        self.cid = str(cid)
        self._get(
            "https://welearn.sflep.com/2019/student/course_info.aspx?cid=" + self.cid,
            headers={"Referer": "https://welearn.sflep.com/2019/student/index.aspx"},
        )
        self.uid, self.classid = welearn_common.get_course_ids(self.session, self.cid)
        self.units = welearn_common.fetch_units(self.session, self.cid, self.uid)
        return self.units

    def fetch_sections(self, unit_index: int) -> List[dict]:
        return welearn_common.fetch_sections(
            self.session, self.cid, self.uid, unit_index - 1, self.classid
        )

    def _read_study_session(self, common_data: dict, headers: dict) -> Dict[str, str]:
        response = self._post(
            AJAX_URL,
            data={**common_data, "action": "getscoinfo_v7"},
            headers=headers,
        )
        if "学习数据不正确" in response.text:
            if not self._wait(self.request_interval):
                raise WelearnError("已停止")
            self._post(
                AJAX_URL,
                data={**common_data, "action": "startsco160928"},
                headers=headers,
            )
            response = self._post(
                AJAX_URL,
                data={**common_data, "action": "getscoinfo_v7"},
                headers=headers,
            )
            if "学习数据不正确" in response.text:
                raise WelearnError("学习数据不正确,无法建立学习会话")
        return self._read_cmi(response)

    def _keepalive(self, common_data: dict, headers: dict,
                   session_time: str, total_time: str):
        return self._post(
            AJAX_URL,
            data={
                **common_data,
                "action": "keepsco_with_getticket_with_updatecmitime",
                "session_time": session_time,
                "total_time": total_time,
            },
            headers=headers,
        )

    def _study_time(self, course: dict, duration: int,
                    headers: dict) -> Optional[Dict[str, str]]:
        common_data = {"uid": self.uid, "cid": self.cid, "scoid": course["id"]}
        cmi = self._read_study_session(common_data, headers)

        if not self._wait(self.request_interval):
            return None
        response = self._keepalive(
            common_data, headers, cmi["session_time"], cmi["total_time"]
        )
        session_seconds = self._read_elapsed_seconds(response)

        self.on_log(f"[学习中]    {course['location']}  {duration} 秒")
        for tick in range(1, duration + 1):
            if not self._wait(1):
                return None
            if tick % KEEPALIVE_EVERY == 0:
                self._keepalive(
                    common_data, headers, cmi["session_time"], cmi["total_time"]
                )

        if duration:
            response = self._keepalive(
                common_data, headers, cmi["session_time"], cmi["total_time"]
            )
            session_seconds = max(session_seconds, self._read_elapsed_seconds(response))
        session_seconds = max(session_seconds, duration)
        return {
            **cmi,
            "session_time": self._add_seconds(cmi["session_time"], session_seconds),
            "total_time": self._add_seconds(cmi["total_time"], session_seconds),
        }

    def _submit_section(self, course: dict, duration: int,
                        accuracy: Optional[int],
                        headers: dict) -> Optional[Dict[str, Any]]:
        cmi = self._study_time(course, duration, headers)
        if cmi is None:
            return None

        common_data = {"uid": self.uid, "cid": self.cid, "scoid": course["id"]}
        setsco_ok: Optional[bool] = None
        if accuracy is None:
            crate = cmi["crate"]
            cstatus = cmi["cstatus"]
            progress = cmi["progress"]
            if not crate:
                course_crate = str(course.get("crate") or "").strip()
                if course_crate not in {"", "-", "无"}:
                    crate = course_crate
            if self._is_complete(course) and cstatus in {"", "unknown", "not_attempted"}:
                cstatus = "completed"
                progress = progress if progress not in {"", "0"} else "100"
        else:
            crate = str(accuracy)
            cstatus = "completed"
            progress = "100"
            self._post(
                AJAX_URL,
                data={**common_data, "action": "startsco160928"},
                headers=headers,
            )
            data = (
                SCO_DATA_TEMPLATE
                .replace("{crate}", crate)
                .replace("{session_time}", cmi["session_time"])
                .replace("{total_time}", cmi["total_time"])
            )
            response = self._post(
                AJAX_URL,
                data={
                    **common_data,
                    "action": "setscoinfo",
                    "data": data,
                    "isend": "False",
                },
                headers=headers,
            )
            setsco_ok = self._response_ok(response)

        response = self._post(
            AJAX_URL,
            data={
                **common_data,
                "action": "savescoinfo160928",
                "progress": progress,
                "crate": crate,
                "status": "unknown",
                "cstatus": cstatus,
                "trycount": "0",
            },
            headers=headers,
        )
        return {
            "crate": crate,
            "duration": duration,
            "setsco_ok": setsco_ok,
            "savesco_ok": self._response_ok(response),
        }

    def run_study(self, units, duration: NumericSpan,
                  accuracy: Optional[NumericSpan] = None,
                  sections=None,
                  section_gap: NumericSpan = (0, 0),
                  skip_completed: bool = False,
                  probe_all_units: bool = False,
                  parallel_workers: int = 1) -> Dict[str, Any]:
        try:
            workers = int(parallel_workers)
        except (TypeError, ValueError):
            raise ValueError("并发小节数必须是整数")
        if not 1 <= workers <= MAX_PARALLEL_WORKERS:
            raise ValueError(f"并发小节数必须在 1~{MAX_PARALLEL_WORKERS} 之间")

        all_units, unit_list, section_map = welearn_common.normalize_targets(units, sections)
        info_headers = {
            "Referer": f"https://welearn.sflep.com/2019/student/course_info.aspx?cid={self.cid}"
        }
        stats: Dict[str, Any] = {
            "total": 0,
            "succeed": 0,
            "failed": 0,
            "errors": [],
            "skipped": 0,
            "accuracy_submitted": accuracy is not None,
            "way1_succeed": 0,
            "way1_failed": 0,
            "way2_succeed": 0,
            "way2_failed": 0,
        }
        stats_lock = threading.Lock()
        pending: List[Tuple[dict, dict]] = []
        sections_found = 0

        def process_unit(unit_idx: int, unit_no: int) -> bool:
            nonlocal sections_found
            response = self._get(
                "https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"
                f"&cid={self.cid}&uid={self.uid}&unitidx={unit_idx}&classid={self.classid}",
                headers=info_headers,
            )
            if "异常" in response.text or "出错了" in response.text:
                return False

            wanted = section_map.get(unit_no) if section_map else None
            for section_no, course in enumerate(response.json()["info"], start=1):
                if self._stopped():
                    return True
                if wanted and section_no not in wanted:
                    continue
                if wanted:
                    sections_found += 1
                if str(course.get("isvisible", "true")).lower() == "false":
                    stats["skipped"] += 1
                    self.on_log(f'[!!跳过!!]    {course["location"]}')
                    continue
                if skip_completed and self._is_complete(course):
                    stats["skipped"] += 1
                    self.on_log(f'[ 已完成 ]    {course["location"]}')
                    continue

                headers = {
                    "Referer": (
                        "https://welearn.sflep.com/Student/StudyCourse.aspx"
                        f"?cid={self.cid}&classid={self.classid}&sco={course['id']}"
                    )
                }
                pending.append((course, headers))
            return True

        def submit_item(item: Tuple[dict, dict]) -> None:
            course, headers = item
            if self._stopped():
                return

            sampled_duration = self._sample_span(duration)
            sampled_accuracy = (
                self._sample_span(accuracy) if accuracy is not None else None
            )
            if accuracy is not None:
                self.on_log(
                    f'[即将完成]    {course["location"]}  '
                    f'学习 {sampled_duration} 秒后提交正确率 {sampled_accuracy}%'
                )
            else:
                self.on_log(f'章节 : {course["location"]}')
                self.on_log(
                    f'已学 : {course.get("learntime", "00:00:00")} '
                    f'将学 : {sampled_duration}'
                )

            try:
                result = self._submit_section(
                    course, sampled_duration, sampled_accuracy, headers
                )
            except WelearnError as error:
                if str(error) == "已停止":
                    return
                with stats_lock:
                    stats["total"] += 1
                    stats["failed"] += 1
                    stats["errors"].append(course["location"])
                    stats["way1_failed"] += 1
                    stats["way2_failed"] += 1
                self.on_log(f'[!] {course["location"]}: {error}')
                return
            if result is None:
                return

            with stats_lock:
                stats["total"] += 1
                if result["setsco_ok"] is True:
                    stats["way1_succeed"] += 1
                elif result["setsco_ok"] is False:
                    stats["way1_failed"] += 1
                if result["savesco_ok"]:
                    stats["succeed"] += 1
                    stats["way2_succeed"] += 1
                else:
                    stats["failed"] += 1
                    stats["errors"].append(course["location"])
                    stats["way2_failed"] += 1

            if accuracy is not None:
                self.on_log(
                    f'>>>>>>>>>>>>>>正确率:{str(sampled_accuracy):>3}%  '
                    f'方式1:{"成功!!!" if result["setsco_ok"] else "失败!!!"}  '
                    f'方式2:{"成功!!!" if result["savesco_ok"] else "失败!!!"}'
                )
            else:
                self.on_log(f'[ 完成 ]    {course["location"]}')

            gap = self._sample_span(section_gap)
            if gap:
                self._wait(gap)

        if all_units and probe_all_units:
            unit_idx = 0
            while not self._stopped() and process_unit(unit_idx, unit_idx + 1):
                unit_idx += 1
        else:
            indices = range(len(self.units)) if all_units else [
                unit_no - 1 for unit_no in unit_list
            ]
            for unit_idx in indices:
                if self._stopped():
                    break
                if not process_unit(unit_idx, unit_idx + 1):
                    if not all_units:
                        self.on_log(f"[!] 单元 {unit_idx + 1} 不可用,已跳过")

        if workers == 1:
            for item in pending:
                submit_item(item)
        elif pending:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(submit_item, pending))

        if section_map and sections_found == 0:
            self.on_log("[!] 未找到任何指定小节(编号超出所选单元的小节范围)")
        return stats
