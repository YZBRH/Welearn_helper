import json
import threading
import time

from tests.fakes import FakeResponse, FakeSession
import welearn_study
import welearn_time


def make_task(fake, chapters):
    task = welearn_time.TimeTask("u", "p", on_log=lambda m: None)
    task.session = fake
    task.cid, task.uid, task.classid = "C1", "U1", "K1"
    task.units = [{"unitname": "U1", "name": "Unit One"}]
    task.request_interval = 0
    fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
        FakeResponse(json_data={"info": chapters})
    return task


def ok_ajax_response():
    cmi = {"score": {"scaled": ""}, "completion_status": "unknown",
           "progress_measure": "0", "total_time": "0", "session_time": "0"}
    return FakeResponse(json_data={"comment": json.dumps({"cmi": cmi})})


class TestFetchUnits:
    def test_reuses_provided_session(self):
        session = object()
        task = welearn_time.TimeTask("u", "p", session=session)
        assert task.session is session

    def test_keeps_original_request_sequence(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/2019/student/course_info.aspx"] = \
            FakeResponse(text="page")
        fake.routes["https://welearn.sflep.com/student/course_info.aspx"] = FakeResponse(
            text='var d = {"uid":7,"n":1}; "classid":"K9"')
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx"] = \
            FakeResponse(json_data={"info": []})   # courseunits 走 params,FakeSession 记录裸 URL
        task = welearn_time.TimeTask("u", "p", on_log=lambda m: None)
        task.session = fake
        task.fetch_units("55")
        urls = [r[1] for r in fake.requests]
        i2019 = urls.index("https://welearn.sflep.com/2019/student/course_info.aspx?cid=55")
        iparse = urls.index("https://welearn.sflep.com/student/course_info.aspx?cid=55")
        assert i2019 < iparse, "原版先请求 /2019/ 页面(响应弃用)再请求解析页"


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

    def test_preserves_existing_accuracy_and_status(self):
        fake = FakeSession()
        cmi = {
            "score": {"scaled": "87"},
            "completion_status": "completed",
            "progress_measure": "1",
            "total_time": "00:20:00",
            "session_time": "00:05:00",
        }
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(
            json_data={"ret": 0, "comment": json.dumps({"cmi": cmi})}
        )
        task = make_task(fake, [
            {"id": 1, "location": "A", "learntime": "00:20:00",
             "isvisible": "true", "iscomplete": "已完成"},
        ])
        result = task.run(1, 0)

        assert result["succeed"] == 1
        posts = [r[2] for r in fake.requests
                 if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"]
        actions = [post["action"] for post in posts]
        assert "setscoinfo" not in actions
        save = next(post for post in posts if post["action"] == "savescoinfo160928")
        assert save["crate"] == "87"
        assert save["cstatus"] == "completed"
        assert save["progress"] == "1"

    def test_bad_sco_recorded_as_error(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = \
            FakeResponse(text="学习数据不正确")
        chapters = [{"id": 1, "location": "BadChapter", "learntime": 0}]
        task = make_task(fake, chapters)
        result = task.run(1, 1)
        assert result["failed"] == 1
        assert result["errors"] == ["BadChapter"]

    def test_submits_accuracy_after_study_duration(self):
        fake = FakeSession()

        def sco_response(url, data):
            action = data["action"]
            if action == "getscoinfo_v7":
                return FakeResponse(
                    json_data={
                        "comment": json.dumps({
                            "cmi": {
                                "session_time": "00:00:04",
                                "total_time": "00:01:00",
                            }
                        })
                    }
                )
            if action == "keepsco_with_getticket_with_updatecmitime":
                return FakeResponse(json_data={"ret": 0, "seconds": 0})
            return FakeResponse(text='{"ret":0}')

        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = sco_response
        task = make_task(fake, [
            {"id": 1, "location": "A", "learntime": 0,
             "isvisible": "true", "iscomplete": "未完成"},
        ])
        task.accuracy_section_gap_seconds = (0, 0)

        result = task.run(1, 1, accuracy=88)

        assert result["accuracy_submitted"] is True
        assert result["way1_succeed"] == 1
        assert result["way2_succeed"] == 1
        actions = [
            request[2]["action"] for request in fake.requests
            if request[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"
        ]
        assert actions == [
            "getscoinfo_v7",
            "keepsco_with_getticket_with_updatecmitime",
            "keepsco_with_getticket_with_updatecmitime",
            "startsco160928",
            "setscoinfo",
            "savescoinfo160928",
        ]
        setsco_data = next(
            request[2]["data"] for request in fake.requests
            if request[2] and request[2].get("action") == "setscoinfo"
        )
        cmi = json.loads(setsco_data.split("[INTERACTIONINFO]")[0])["cmi"]
        assert cmi["score"]["scaled"] == "88"
        assert cmi["session_time"] == "00:00:05"
        assert cmi["total_time"] == "00:01:01"

    def test_accuracy_only_keeps_original_default_duration(self):
        task = welearn_time.TimeTask("u", "p", on_log=lambda m: None)
        captured = {}

        def fake_run_study(units, **kwargs):
            captured.update(units=units, **kwargs)
            return {
                "total": 0,
                "succeed": 0,
                "failed": 0,
                "errors": [],
                "accuracy_submitted": kwargs.get("accuracy") is not None,
                "way1_succeed": 0,
                "way1_failed": 0,
                "way2_succeed": 0,
                "way2_failed": 0,
            }

        task.run_study = fake_run_study
        task.run(1, None, accuracy=88)

        assert captured["duration"] == welearn_study.DEFAULT_STUDY_SECONDS
        assert captured["accuracy"] == 88
        assert captured["skip_completed"] is True
        assert captured["probe_all_units"] is True

    def test_explicit_duration_processes_completed_sections(self):
        task = welearn_time.TimeTask("u", "p", on_log=lambda m: None)
        captured = {}

        def fake_run_study(units, **kwargs):
            captured.update(units=units, **kwargs)
            return {
                "total": 0,
                "succeed": 0,
                "failed": 0,
                "errors": [],
                "accuracy_submitted": kwargs.get("accuracy") is not None,
                "way1_succeed": 0,
                "way1_failed": 0,
                "way2_succeed": 0,
                "way2_failed": 0,
            }

        task.run_study = fake_run_study
        task.run(1, 30, accuracy=88, parallel_workers=3)

        assert captured["duration"] == 30
        assert captured["accuracy"] == 88
        assert captured["parallel_workers"] == 3
        assert captured["skip_completed"] is False
        assert captured["probe_all_units"] is False

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


class TestSectionFilter:
    def test_section_index_targets_single_chapter(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = ok_ajax_response()
        chapters = [{"id": 1, "location": "A", "learntime": 0},
                    {"id": 2, "location": "B", "learntime": 0},
                    {"id": 3, "location": "C", "learntime": 0}]
        task = make_task(fake, chapters)
        result = task.run(1, 1, sections=3)
        assert result["total"] == 1
        assert result["succeed"] == 1
        save_posts = [r for r in fake.requests
                      if r[2] and r[2].get("action") == "savescoinfo160928"]
        assert len(save_posts) == 1
        assert save_posts[0][2]["scoid"] == 3

    def test_section_with_all_units_rejected(self):
        import pytest
        task = welearn_time.TimeTask("u", "p", on_log=lambda m: None)
        with pytest.raises(ValueError):
            task.run(0, 30, sections=1)


class TestMultiSelect:
    def test_multi_units_multi_sections(self):
        fake = FakeSession()

        def scoleaves(url, data):
            unitidx = int(url.split("unitidx=")[1].split("&")[0])
            info = [{"id": 100 + unitidx, "location": "A", "learntime": 0},
                    {"id": 200 + unitidx, "location": "B", "learntime": 0}]
            return FakeResponse(json_data={"info": info})

        task = make_task(fake, [])   # 先建任务,再用可调用路由覆盖其静态路由
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = scoleaves
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = ok_ajax_response()
        task.units = [{}] * 3
        result = task.run([2, 3], 1, sections={2: [1], 3: [2]})
        # 单元2(unitidx=1)取第1小节(id=101),单元3(unitidx=2)取第2小节(id=202)
        assert result["total"] == 2
        saved = [r[2]["scoid"] for r in fake.requests
                 if r[2] and r[2].get("action") == "savescoinfo160928"]
        assert sorted(saved) == [101, 202]


class TestParallelSections:
    def test_two_workers_overlap_study_timers(self):
        fake = FakeSession()
        task = make_task(fake, [])
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
            FakeResponse(json_data={"info": [
                {"id": i, "location": f"L{i}", "learntime": 0,
                 "isvisible": "true", "iscomplete": "未完成"}
                for i in range(1, 5)
            ]})
        task.units = [{"unitname": "U1", "name": "Unit One"}]
        barrier = threading.Barrier(2)
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_submit(course, duration, accuracy, headers):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            barrier.wait(timeout=2)
            with lock:
                active -= 1
            return {
                "crate": "",
                "duration": duration,
                "setsco_ok": None,
                "savesco_ok": True,
            }

        task._submit_section = fake_submit
        result = task.run(1, 1, parallel_workers=2)

        assert max_active == 2, result
        assert result["total"] == 4
        assert result["succeed"] == 4

    def test_rejects_workers_outside_supported_range(self):
        import pytest
        task = welearn_time.TimeTask("u", "p", on_log=lambda m: None)
        task.units = [{}]
        with pytest.raises(ValueError, match="1~4"):
            task.run(1, 1, parallel_workers=5)
