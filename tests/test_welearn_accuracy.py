import json

from tests.fakes import FakeResponse, FakeSession
import welearn_accuracy


def make_task(fake, chapters):
    task = welearn_accuracy.AccuracyTask(
        "u", "p", on_log=lambda m: None,
        study_seconds=(0, 0), section_gap_seconds=(0, 0), request_interval=0)
    task.session = fake
    task.cid, task.uid, task.classid = "C1", "U1", "K1"
    task.units = [{"unitname": "U1", "name": "Unit One", "visible": "true"}]

    def scoleaves(url, data):
        # 复刻真实服务器语义:unitidx=0 返回单元数据,越界(>=1)返回错误文本终止遍历
        if "unitidx=0" in url:
            return FakeResponse(json_data={"info": chapters})
        return FakeResponse(text="出错了")

    fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = scoleaves
    return task


class TestUnitIteration:
    def test_reuses_provided_session(self):
        session = object()
        task = welearn_accuracy.AccuracyTask("u", "p", session=session)
        assert task.session is session

    def test_all_units_run_until_server_error(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":0}')

        task = make_task(fake, chapters=[])   # units 长度 1

        def scoleaves(url, data):
            if "unitidx=1" in url:
                return FakeResponse(text="出错了")
            return FakeResponse(json_data={"info": [
                {"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"}]})

        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = scoleaves
        result = task.run(0, 100)
        urls = [r[1] for r in fake.requests]
        assert any("unitidx=1" in u for u in urls), "原版会持续探测直到服务器报错才终止"
        assert result["way1_succeed"] == 1


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
        actions = [r[2].get("action") for r in fake.requests
                   if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"]
        assert actions == [
            "getscoinfo_v7",
            "keepsco_with_getticket_with_updatecmitime",
            "startsco160928",
            "setscoinfo",
            "savescoinfo160928",
        ]

    def test_failure_counters(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":1}')
        chapters = [{"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"}]
        task = make_task(fake, chapters)
        result = task.run(1, (70, 100))
        assert result["way1_failed"] == 1
        assert result["way2_failed"] == 1
        data = next(r[2] for r in fake.requests
                    if r[2] and r[2].get("action") == "setscoinfo")
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


class TestFetchSections:
    def test_unit_index_is_one_based(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
            FakeResponse(json_data={"info": [{"location": "L1"}]})
        task = welearn_accuracy.AccuracyTask(
            "u", "p", on_log=lambda m: None,
            study_seconds=(0, 0), section_gap_seconds=(0, 0), request_interval=0)
        task.session = fake
        task.cid, task.uid, task.classid = "2833", "9", "K"
        info = task.fetch_sections(3)
        assert info[0]["location"] == "L1"
        assert "unitidx=2" in fake.requests[0][1]


class TestSectionFilter:
    def test_section_index_brushes_only_that_section(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":0}')
        chapters = [
            {"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"},
            {"id": 2, "location": "L2", "isvisible": "true", "iscomplete": "未完成"},
            {"id": 3, "location": "L3", "isvisible": "true", "iscomplete": "未完成"},
        ]
        task = make_task(fake, chapters)
        result = task.run(1, 100, sections=2)   # 只刷第2小节
        assert result["way1_succeed"] == 1
        sco_posts = [r for r in fake.requests if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"]
        assert [r[2].get("action") for r in sco_posts if r[2].get("action") in (
            "startsco160928", "setscoinfo", "savescoinfo160928"
        )] == ["startsco160928", "setscoinfo", "savescoinfo160928"]
        assert all(r[2]["scoid"] == 2 for r in sco_posts)

    def test_section_over_range_logs_hint(self):
        logs = []
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":0}')
        task = welearn_accuracy.AccuracyTask(
            "u", "p", on_log=logs.append,
            study_seconds=(0, 0), section_gap_seconds=(0, 0), request_interval=0)
        task.session = fake
        task.cid, task.uid, task.classid = "C1", "U1", "K1"
        task.units = [{"unitname": "U1", "name": "Unit One"}]
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
            FakeResponse(json_data={"info": [{"id": 1, "location": "L1",
                                              "isvisible": "true", "iscomplete": "未完成"}]})
        result = task.run(1, 100, sections=9)
        assert result["way1_succeed"] == 0
        sco_posts = [r for r in fake.requests if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"]
        assert len(sco_posts) == 0
        assert any("未找到任何指定小节" in m for m in logs)

    def test_section_with_all_units_rejected(self):
        import pytest
        task = welearn_accuracy.AccuracyTask(
            "u", "p", on_log=lambda m: None,
            study_seconds=(0, 0), section_gap_seconds=(0, 0), request_interval=0)
        with pytest.raises(ValueError):
            task.run(0, 100, sections=1)


class TestMultiSelect:
    def test_multiple_units_all_processed(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":0}')

        def scoleaves(url, data):
            if "unitidx=0" in url or "unitidx=2" in url:
                return FakeResponse(json_data={"info": [
                    {"id": 1, "location": "L", "isvisible": "true", "iscomplete": "未完成"}]})
            return FakeResponse(text="出错了")

        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = scoleaves
        task = welearn_accuracy.AccuracyTask(
            "u", "p", on_log=lambda m: None,
            study_seconds=(0, 0), section_gap_seconds=(0, 0), request_interval=0)
        task.session = fake
        task.cid, task.uid, task.classid = "C1", "U1", "K1"
        task.units = [{}] * 3
        result = task.run([1, 3], 100)
        urls = [r[1] for r in fake.requests if "scoLeaves" in r[1]]
        assert any("unitidx=0" in u for u in urls) and any("unitidx=2" in u for u in urls)
        assert result["way1_succeed"] == 2   # 两个单元各刷 1 小节
        assert not any("unitidx=1" in u for u in urls), "未选中的单元不应请求"

    def test_multi_sections_within_unit(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":0}')
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
            FakeResponse(json_data={"info": [
                {"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"},
                {"id": 2, "location": "L2", "isvisible": "true", "iscomplete": "未完成"},
                {"id": 3, "location": "L3", "isvisible": "true", "iscomplete": "未完成"},
                {"id": 4, "location": "L4", "isvisible": "true", "iscomplete": "未完成"}]})
        task = welearn_accuracy.AccuracyTask(
            "u", "p", on_log=lambda m: None,
            study_seconds=(0, 0), section_gap_seconds=(0, 0), request_interval=0)
        task.session = fake
        task.cid, task.uid, task.classid = "C1", "U1", "K1"
        task.units = [{}]
        result = task.run([1], 100, sections={1: [1, 3]})
        assert result["way1_succeed"] == 2
        scoids = {r[2]["scoid"] for r in fake.requests
                  if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx" and r[2]["action"] == "startsco160928"}
        assert scoids == {1, 3}

    def test_empty_unit_list_rejected(self):
        import pytest
        task = welearn_accuracy.AccuracyTask("u", "p", on_log=lambda m: None)
        with pytest.raises(ValueError):
            task.run([], 100)

    # 边界输入的其余校验见 tests/test_welearn_common.py::TestNormalizeTargets
    # (纯函数测试;经 AccuracyTask 会让未 mock 的真实 session 发网络请求,不可用)


class TestCompletionPersistence:
    def test_submits_real_study_time_before_score(self):
        fake = FakeSession()

        def sco_response(url, data):
            action = data["action"]
            if action == "getscoinfo_v7":
                return FakeResponse(
                    json_data={"comment": '{"cmi":{"session_time":"00:12:00",'
                                          '"total_time":"00:40:00"}}'})
            if action == "keepsco_with_getticket_with_updatecmitime":
                return FakeResponse(json_data={"ret": 0, "seconds": 7})
            return FakeResponse(text='{"ret":0}')

        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = sco_response
        task = make_task(fake, [
            {"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"},
        ])
        task.study_seconds = (1, 1)
        result = task.run(1, 88)

        assert result["way1_succeed"] == 1
        assert result["way2_succeed"] == 1
        posts = [r[2] for r in fake.requests
                 if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx" and r[2]]
        assert [r["action"] for r in posts] == [
            "getscoinfo_v7",
            "keepsco_with_getticket_with_updatecmitime",
            "keepsco_with_getticket_with_updatecmitime",
            "startsco160928",
            "setscoinfo",
            "savescoinfo160928",
        ]
        setsco_data = posts[4]["data"].split("[INTERACTIONINFO]")[0]
        cmi = json.loads(setsco_data)["cmi"]
        assert cmi["score"]["scaled"] == "88"
        assert cmi["session_time"] == "00:12:07"
        assert cmi["total_time"] == "00:40:07"

    def test_stop_during_study_does_not_submit(self):
        import threading
        import time

        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(
            json_data={"comment": '{"cmi":{"session_time":"1","total_time":"2"}}'})
        task = make_task(fake, [
            {"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"},
        ])
        task.study_seconds = (5, 5)
        threading.Timer(0.05, task.stop_event.set).start()
        start = time.time()
        result = task.run(1, 100)

        assert time.time() - start < 2
        assert result["way1_succeed"] == 0
        actions = [r[2].get("action") for r in fake.requests
                   if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"]
        assert "setscoinfo" not in actions
        assert "savescoinfo160928" not in actions
