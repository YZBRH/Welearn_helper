import json
import random
import threading
import time

import pytest

import welearn_flow
from tests.fakes import FakeResponse, FakeSession
from welearn_flow import FlowTask, sample_span, to_span


def cmi_response(total_time="0", session_time="0"):
    cmi = {"score": {"scaled": "60"}, "completion_status": "incomplete",
           "progress_measure": "0.5", "total_time": total_time, "session_time": session_time}
    return FakeResponse(json_data={"comment": json.dumps({"cmi": cmi})})


def make_task(sections, request_interval=0, units=None):
    fake = FakeSession()
    fake.routes["https://welearn.sflep.com/2019/student/course_info.aspx"] = \
        FakeResponse(text="page")
    fake.routes["https://welearn.sflep.com/student/course_info.aspx"] = FakeResponse(
        text='var d = {"uid":7,"n":1}; "classid":"K9"')
    fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
        FakeResponse(json_data={"info": sections})
    if units is None:
        units = [{"unitname": "U1", "name": "第一单元", "visible": "true"}]
    fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx"] = \
        FakeResponse(json_data={"info": units})
    fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = cmi_response()
    task = FlowTask("u", "p", on_log=lambda m: None, request_interval=request_interval)
    task.session = fake
    return task, fake


def job(**kwargs):
    base = {"no": 1, "cid": "C1", "name": "课A", "accuracy": 88,
            "duration": 0, "pause": 0, "skip_done": True}
    base.update(kwargs)
    return base


def section(sid, complete="未完成", visible="true"):
    return {"id": sid, "location": f"L{sid}", "iscomplete": complete, "isvisible": visible}


def actions_of(fake):
    return [r[2].get("action") for r in fake.requests if r[2]]


class TestParamParsing:
    def test_to_span_accepts_int_list_and_text(self):
        assert to_span(88) == 88
        assert to_span([70, 95]) == (70, 95)
        assert to_span("70,95") == (70, 95)
        assert to_span(" 90 ") == 90

    def test_to_span_rejects_reversed_range(self):
        with pytest.raises(ValueError):
            to_span([95, 70])

    def test_sample_span_within_range(self):
        random.seed(7)
        values = {sample_span((70, 75)) for _ in range(60)}
        assert values and values <= set(range(70, 76))
        assert len(values) > 1          # 区间模式必须逐节随机取样

    def test_sample_span_constant(self):
        assert {sample_span(30) for _ in range(5)} == {30}


class TestNormalizeJob:
    def test_defaults_filled(self):
        normalized = welearn_flow.normalize_job({"no": 2, "cid": 9, "accuracy": "70,90",
                                                 "duration": 30})
        assert normalized["cid"] == "9"
        assert normalized["accuracy"] == (70, 90)
        assert normalized["skip_done"] is True
        assert normalized["pause"] == welearn_flow.DEFAULT_PAUSE

    def test_rejects_accuracy_over_100(self):
        with pytest.raises(ValueError):
            welearn_flow.normalize_job({"no": 1, "cid": 1, "accuracy": 120, "duration": 30})

    def test_rejects_negative_duration(self):
        with pytest.raises(ValueError):
            welearn_flow.normalize_job({"no": 1, "cid": 1, "accuracy": 80, "duration": -1})

    def test_rejects_missing_cid(self):
        with pytest.raises(ValueError):
            welearn_flow.normalize_job({"no": 1, "accuracy": 80, "duration": 30})


class TestLearnSection:
    def test_writes_time_and_accuracy_in_one_pass(self):
        task, fake = make_task([section(11)])
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = \
            cmi_response(total_time="125", session_time="125")

        result = task.run_course(job(duration=0))

        assert result["status"] == "done"
        assert result["done"] == 1
        actions = actions_of(fake)
        assert actions.index("getscoinfo_v7") < actions.index(
            "keepsco_with_getticket_with_updatecmitime")
        assert actions.index("keepsco_with_getticket_with_updatecmitime") < \
            actions.index("savescoinfo160928")
        saves = [r[2] for r in fake.requests
                 if r[2] and r[2].get("action") == "savescoinfo160928"]
        assert saves and saves[-1]["crate"] == "88"
        assert saves[-1]["cstatus"] == "completed" and saves[-1]["progress"] == "100"

    def test_accuracy_written_via_setscoinfo_like_accuracy_task(self):
        """正确率必须走 AccuracyTask 的方式1+方式2,只调 savescoinfo 服务端不落分"""
        task, fake = make_task([section(11)])
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = \
            cmi_response(total_time="125", session_time="125")

        task.run_course(job(duration=0))

        actions = actions_of(fake)
        assert "startsco160928" in actions
        assert "setscoinfo" in actions, "正确率必须经 setscoinfo(方式1)写入"
        assert actions.index("startsco160928") < actions.index("setscoinfo")
        assert actions.index("setscoinfo") < actions.index("savescoinfo160928")

    def test_setscoinfo_backfills_time(self):
        """模板里的 "0" 必须回填成最新读数,否则把刚刷的时长清零"""
        task, fake = make_task([section(11)])
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = \
            cmi_response(total_time="125", session_time="125")

        task.run_course(job(duration=0))

        sets = [r[2] for r in fake.requests
                if r[2] and r[2].get("action") == "setscoinfo"]
        assert sets, "应走 setscoinfo(方式1)"
        payload = sets[0]["data"]
        assert '"total_time":"125"' in payload
        assert '"session_time":"125"' in payload
        assert '"total_time":"0"' not in payload
        assert '"scaled":"88"' in payload, "crate 必须写进 cmi 的 score"

    def test_heartbeat_reports_every_interval(self, monkeypatch):
        logs = []
        task, fake = make_task([section(1)])
        task.on_log = logs.append
        monkeypatch.setattr(welearn_flow, "HEARTBEAT_INTERVAL", 0)

        task.run_course(job(duration=3))

        beats = [line for line in logs if line.startswith("[心跳]")]
        assert len(beats) == 3, beats
        assert "本小节 1/3 秒" in beats[0]
        assert "本课程已学" in beats[0] and "本次任务累计" in beats[0]

    def test_bad_sco_recorded_as_failure(self):
        task, fake = make_task([section(11)])
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = \
            FakeResponse(text="学习数据不正确")

        result = task.run_course(job(duration=0))

        assert result["failed"] == 1
        assert "L11" in result["errors"][0]


class TestSkipRules:
    def test_completed_section_skipped_without_requests(self):
        task, fake = make_task([section(11, complete="completed")])

        result = task.run_course(job(duration=0))

        assert result["done"] == 0 and result["skipped"] == 1
        assert "savescoinfo160928" not in actions_of(fake)

    def test_invisible_section_skipped(self):
        task, fake = make_task([section(11, visible="false")])

        result = task.run_course(job(duration=0))

        assert result["done"] == 0 and result["skipped"] == 1
        assert "savescoinfo160928" not in actions_of(fake)

    def test_skip_done_false_relearns_completed_section(self):
        task, fake = make_task([section(11, complete="completed")])

        result = task.run_course(job(duration=0, skip_done=False))

        assert result["done"] == 1


class TestSerialBehaviour:
    def test_sections_do_not_overlap(self):
        stamps = []

        def sco(url, data):
            if data and data.get("action") == "savescoinfo160928":
                stamps.append(time.time())
            return cmi_response()

        task, fake = make_task([section(1), section(2), section(3)])
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = sco
        task.run_course(job(duration=1))

        assert len(stamps) == 3
        gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
        # 每节固定等待 1 秒;若并发执行,相邻保存间隔会远小于 1 秒
        assert all(gap >= 0.9 for gap in gaps), f"小节必须串行,实际间隔 {gaps}"

    def test_stop_interrupts_quickly_and_keeps_partial_result(self):
        task, fake = make_task([section(1)])
        threading.Timer(1.0, task.stop_event.set).start()
        start = time.time()

        result = task.run_course(job(duration=600))

        assert time.time() - start < 5
        assert result["status"] == "stopped"
        assert "savescoinfo160928" not in actions_of(fake)   # 不留半截数据


class TestScopeSelection:
    def test_only_selected_units_are_requested(self):
        units = [{"unitname": f"U{i}", "name": f"单元{i}", "visible": "true"} for i in (1, 2, 3)]
        task, fake = make_task([], units=units)
        requested = []

        def scoleaves(url, data):
            unitidx = int(url.split("unitidx=")[1].split("&")[0])
            requested.append(unitidx)
            return FakeResponse(json_data={"info": [section(100 + unitidx)]})

        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = scoleaves

        result = task.run_course(job(units=[3], duration=0))

        assert requested == [2], "只应请求勾选的单元(1-based 3 → unitidx 2)"
        assert result["done"] == 1

    def test_only_selected_sections_are_written(self):
        task, fake = make_task([section(1), section(2), section(3)])

        result = task.run_course(job(units=[1], sections={1: [2]}, duration=0))

        assert result["done"] == 1
        saved = [r[2]["scoid"] for r in fake.requests
                 if r[2] and r[2].get("action") == "savescoinfo160928"]
        assert saved == [2]

    def test_unit_out_of_range_skips_course(self):
        task, fake = make_task([section(1)])

        result = task.run_course(job(units=[5], duration=0))

        assert result["status"] == "skipped"
        assert result["done"] == 0

    def test_default_scope_is_all_units(self):
        task, fake = make_task([section(1)])

        result = task.run_course(job(duration=0))

        assert result["done"] == 1


class TestQueue:
    def test_runs_in_course_number_order(self):
        logs = []

        def scoleaves(url, data):
            cid = url.split("cid=")[1].split("&")[0]
            return FakeResponse(json_data={"info": [section(int(cid))]})

        task, fake = make_task([])
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = scoleaves
        task.on_log = logs.append

        summary = task.run_queue([job(no=3, cid="103", name="课C"),
                                  job(no=1, cid="101", name="课A"),
                                  job(no=2, cid="102", name="课B")])

        order = [line.split("NO.")[1].split(" ")[0] for line in logs if "NO." in line]
        assert order == ["1", "2", "3"]
        assert summary["total"] == 3 and summary["done"] == 3
        assert [c["no"] for c in summary["courses"]] == [1, 2, 3]

    def test_skip_event_stops_current_course_only(self):
        def scoleaves(url, data):
            cid = url.split("cid=")[1].split("&")[0]
            return FakeResponse(json_data={"info": [section(int(cid)),
                                                    section(int(cid) + 500)]})

        task, fake = make_task([])
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = scoleaves
        original = task.run_course
        calls = {"n": 0}

        def first_course_then_skip(j, course_pos=1, course_total=1):
            calls["n"] += 1
            if calls["n"] == 1:
                task.skip_event.set()
            return original(j, course_pos, course_total)

        task.run_course = first_course_then_skip
        summary = task.run_queue([job(no=1, cid="101"), job(no=2, cid="201")])

        # skip_event 在每门课开始时被清除,故第二门课照常执行
        assert summary["total"] == 2
        assert summary["courses"][1]["done"] == 2

    def test_error_in_one_course_does_not_stop_queue(self):
        task, fake = make_task([])
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
            FakeResponse(text="出错了")

        summary = task.run_queue([job(no=1, cid="101"), job(no=2, cid="102")])

        assert summary["total"] == 2
        assert summary["done"] == 0
