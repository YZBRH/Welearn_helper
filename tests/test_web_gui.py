import threading
import time

import pytest

import web_gui
import welearn_accuracy
import welearn_common
import welearn_time


class FakeTask:
    """替代 AccuracyTask:复刻控制器契约"""

    instances = []

    def __init__(self, username, password, on_log=None, stop_event=None,
                 session=None):
        self.username = username
        self.password = password
        self.on_log = on_log
        self.stop_event = stop_event
        self.session = session or object()
        self.cid = ""
        self.uid = ""
        self.classid = ""
        self.units = []
        FakeTask.instances.append(self)

    def login(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise welearn_common.WelearnError("已停止")

    def fetch_courses(self):
        return [{"cid": 11, "per": 60, "name": "课程A"}]

    def fetch_units(self, cid):
        return [{"unitname": "U1", "name": "第一单元", "visible": "true"}]

    def fetch_sections(self, unit_index):
        return [{"location": "U1 > A", "iscomplete": "completed", "isvisible": "true"},
                {"location": "U1 > B", "iscomplete": "未完成", "isvisible": "false"}]

    def run(self, units, param, sections=None):
        self.last_run_args = (units, param, sections)
        time.sleep(0.2)   # 让"任务运行中"状态可被观测,避免与断言竞态
        self.on_log(f"fake run units={units} param={param} sections={sections}")
        return {"way1_succeed": 1, "way1_failed": 0,
                "way2_succeed": 1, "way2_failed": 0}


class RaisingFakeTask(FakeTask):
    def login(self):
        raise welearn_common.LoginError("帐号或密码错误!")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(welearn_accuracy, "AccuracyTask", FakeTask)
    FakeTask.instances = []
    app, state = web_gui.create_app()
    return app.test_client(), state


def login(client):
    c, _state = client
    return c.post("/api/login", json={"username": "u", "password": "p",
                                      "mode": "accuracy"})


class TestIndex:
    def test_serves_page_with_authors(self, client):
        c, _ = client
        r = c.get("/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Avenshy" in body and "lxbsdsg" in body


class TestLogin:
    def test_empty_credentials_rejected(self, client):
        c, _ = client
        r = c.post("/api/login", json={"username": "", "password": ""})
        assert r.status_code == 400
        assert r.get_json()["ok"] is False

    def test_success_returns_courses(self, client):
        r = login(client)
        assert r.status_code == 200
        j = r.get_json()
        assert j["ok"] is True and len(j["courses"]) == 1

    def test_login_after_stop_succeeds(self, client):
        c, state = client
        state.stop_event.set()          # 模拟此前点过"停止"
        j = login(client).get_json()
        assert j["ok"] is True

    def test_login_failure_returns_error(self, monkeypatch):
        monkeypatch.setattr(welearn_accuracy, "AccuracyTask", RaisingFakeTask)
        FakeTask.instances = []
        app, _state = web_gui.create_app()
        r = app.test_client().post("/api/login",
                                   json={"username": "u", "password": "bad"})
        j = r.get_json()
        assert j["ok"] is False and j["error"] == "帐号或密码错误!"


class TestUnits:
    def test_requires_login(self, client):
        c, _ = client
        r = c.post("/api/units", json={"cid": 11})
        assert r.status_code == 400

    def test_after_login(self, client):
        c, _ = client
        login(client)
        r = c.post("/api/units", json={"cid": 11})
        j = r.get_json()
        assert j["ok"] is True and len(j["units"]) == 1


class TestStartStop:
    def test_invalid_param_rejected(self, client):
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        r = c.post("/api/start", json={"unit_index": 0, "param": "abc"})
        assert r.status_code == 400
        assert "参数格式错误" in r.get_json()["error"]

    def test_start_runs_task_and_reports(self, client):
        c, state = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        r = c.post("/api/start", json={"unit_index": 0, "param": "100"})
        assert r.get_json()["ok"] is True
        assert state.running is True
        state.worker.join(timeout=5)
        assert state.running is False
        assert state.last_result["way1_succeed"] == 1
        r = c.get("/api/status")
        j = r.get_json()
        assert j["running"] is False
        assert any("fake run" in m for _s, _t, m in j["logs"])


class TestLogout:
    def test_logout_clears_task(self, client):
        """退出登录必须作废服务端旧 task"""
        c, state = client
        login(client)
        assert state.task is not None
        r = c.post("/api/logout")
        assert r.get_json()["ok"] is True
        assert state.task is None and state.units == []
        j = c.get("/api/status").get_json()
        assert j["has_task"] is False
        r = c.post("/api/start", json={"unit_index": 0, "param": "100"})
        assert r.status_code == 400


class TestModeSwitch:
    def test_reuses_session_and_course_context(self, client, monkeypatch):
        monkeypatch.setattr(welearn_time, "TimeTask", FakeTask)
        c, state = client
        login(client)
        old_task = state.task
        old_task.cid, old_task.uid, old_task.classid = "11", "7", "K9"
        old_task.units = [{"unitname": "U1", "name": "第一单元"}]
        state.units = old_task.units
        state.sections_map = {1: [{"location": "U1 > A"}]}

        r = c.post("/api/mode", json={"mode": "time"})
        j = r.get_json()
        assert r.status_code == 200 and j["ok"] is True and j["mode"] == "time"
        assert state.mode == "time"
        assert state.task is not old_task
        assert state.task.session is old_task.session
        assert (state.task.cid, state.task.uid, state.task.classid) == ("11", "7", "K9")
        assert state.task.units == old_task.units
        assert state.courses and state.units == old_task.units
        assert state.sections_map == {1: [{"location": "U1 > A"}]}

    def test_same_mode_does_not_create_task(self, client):
        c, state = client
        login(client)
        instance_count = len(FakeTask.instances)
        r = c.post("/api/mode", json={"mode": "accuracy"})
        assert r.get_json() == {"ok": True, "mode": "accuracy"}
        assert len(FakeTask.instances) == instance_count

    def test_rejects_switch_while_running(self, client):
        c, state = client
        login(client)
        state.running = True
        r = c.post("/api/mode", json={"mode": "time"})
        assert r.status_code == 409
        assert state.mode == "accuracy"


class TestPageJs:
    def test_alert_strings_keep_literal_backslash_n(self, client):
        """INDEX_HTML 若非 raw 字符串,JS 里的 \\n 会被 Python 先转义成真换行,
        字符串字面量被折断 → 浏览器 Uncaught SyntaxError(用户实测踩中)"""
        c, _ = client
        body = c.get("/").get_data(as_text=True)
        assert "alert('登录失败\\n'" in body   # 必须是字面量 反斜杠+n

    def test_mode_switch_reuses_login_without_logout(self, client):
        c, _ = client
        body = c.get("/").get_data(as_text=True)
        selector = "document.querySelectorAll('input[name=mode]')"
        first = body.index(selector)
        start = body.index(selector, first + len(selector))
        end = body.index("$('loginBtn')", start)
        handler = body[start:end]
        assert "/api/mode" in handler
        assert "/api/logout" not in handler
        assert "无需重新登录" in handler
        assert "fillCourses([])" not in handler
        assert "resetTree()" not in handler


class TestQuietStatusPoll:
    def test_status_poll_lines_hidden_others_kept(self):
        """/api/status 每秒轮询会刷屏控制台,需隐藏;其余访问行保留"""
        import logging
        f = web_gui._QuietStatusPoll()

        def mk(msg):
            return logging.LogRecord("werkzeug", logging.INFO, "p", 1, msg, None, None)

        assert f.filter(mk('127.0.0.1 - - "GET /api/status?after=6 HTTP/1.1" 200 -')) is False
        assert f.filter(mk('127.0.0.1 - - "GET / HTTP/1.1" 200 -')) is True
        assert f.filter(mk('127.0.0.1 - - "POST /api/login HTTP/1.1" 200 -')) is True


class TestSections:
    def test_requires_login(self, client):
        c, _ = client
        r = c.post("/api/sections", json={"unit_index": 1})
        assert r.status_code == 400

    def test_requires_units(self, client):
        c, _ = client
        login(client)
        r = c.post("/api/sections", json={"unit_index": 1})
        assert r.status_code == 400

    def test_returns_parsed_sections(self, client):
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        j = c.post("/api/sections", json={"unit_index": 1}).get_json()
        assert j["ok"] is True
        assert j["sections"][0] == {"location": "U1 > A", "complete": True, "visible": True}
        assert j["sections"][1]["complete"] is False
        assert j["sections"][1]["visible"] is False

    def test_zero_and_out_of_range_rejected(self, client):
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        assert c.post("/api/sections", json={"unit_index": 0}).status_code == 400
        assert c.post("/api/sections", json={"unit_index": 9}).status_code == 400


class TestStartSection:
    def test_start_passes_section_index_to_task(self, client):
        c, state = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        c.post("/api/sections", json={"unit_index": 1})
        j = c.post("/api/start", json={"unit_index": 1, "param": "100",
                                       "section_index": 2}).get_json()
        assert j["ok"] is True
        state.worker.join(timeout=5)
        assert FakeTask.instances[-1].last_run_args == ([1], 100, {1: [2]})

    def test_section_with_all_units_rejected(self, client):
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        r = c.post("/api/start", json={"unit_index": 0, "param": "100",
                                       "section_index": 1})
        assert r.status_code == 400
        assert "具体单元" in r.get_json()["error"]

    def test_section_over_known_range_rejected(self, client):
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        c.post("/api/sections", json={"unit_index": 1})   # 2 个小节
        r = c.post("/api/start", json={"unit_index": 1, "param": "100",
                                       "section_index": 99})
        assert r.status_code == 400
        assert "1~2" in r.get_json()["error"]


class TestRefreshAfterTaskEnd:
    def test_units_not_rejected_while_worker_draining(self, client):
        """任务结束(running已False)但工作线程尚未退出时,刷新单元不得409"""
        c, state = client
        login(client)
        state.worker = threading.Thread(target=time.sleep, args=(1.0,), daemon=True)
        state.worker.start()
        state.running = False   # 模拟 work() 已置 False、线程将退未退
        r = c.post("/api/units", json={"cid": 11})
        assert r.status_code == 200

    def test_login_clears_stale_units_and_sections(self, client):
        """重新登录必须作废上一会话的单元与小节,防止界面残留旧状态"""
        c, state = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        c.post("/api/sections", json={"unit_index": 1})
        assert state.units and state.sections_map
        login(client)
        assert state.units == [] and state.sections_map == {}

    def test_start_rejected_while_fetch_in_flight(self, client):
        """单元/课程拉取进行中(fetching)时不得开任务,避免并发用同一会话"""
        c, state = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        state.fetching = True   # 模拟一次拉取正在进行
        r = c.post("/api/start", json={"unit_index": 1, "param": "100"})
        assert r.status_code == 409

    def test_rejected_start_does_not_wedge_running_state(self, client):
        """409 拒绝时不得提前置 running=True,否则界面永久卡在"任务运行中"且无法恢复"""
        c, state = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        state.fetching = True
        c.post("/api/start", json={"units": [1], "param": "100"})
        assert state.running is False
        j = c.get("/api/status").get_json()
        assert j["running"] is False
        assert not any("开始任务" in m for _s, _t, m in j["logs"])


class TestMultiSelectStart:
    def test_units_list_and_sections_map_passed(self, client):
        c, state = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        j = c.post("/api/start", json={"units": [3, 1], "param": "100",
                                       "sections": {"3": [2, 5]}}).get_json()
        assert j["ok"] is True
        state.worker.join(timeout=5)
        assert FakeTask.instances[-1].last_run_args == ([1, 3], 100, {3: [2, 5]})

    def test_empty_units_rejected(self, client):
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        r = c.post("/api/start", json={"units": [], "param": "100"})
        assert r.status_code == 400
        assert "未选择任何单元" in r.get_json()["error"]

    def test_all_units_with_sections_rejected(self, client):
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        r = c.post("/api/start", json={"units": 0, "param": "100",
                                       "sections": {"1": [1]}})
        assert r.status_code == 400

    def test_sections_over_known_range_rejected_per_unit(self, client):
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        c.post("/api/sections", json={"unit_index": 1})   # 2 个小节
        r = c.post("/api/start", json={"units": [1], "param": "100",
                                       "sections": {"1": [99]}})
        assert r.status_code == 400
        assert "1~2" in r.get_json()["error"]

    def test_sections_key_outside_unit_list_rejected(self, client):
        """小节键指向未勾选单元时必须拒绝,否则该小节被静默丢弃而整单元被刷"""
        c, _ = client
        login(client)
        c.post("/api/units", json={"cid": 11})
        r = c.post("/api/start", json={"units": [1], "param": "100",
                                       "sections": {"5": [1]}})
        assert r.status_code == 400


class TestStatusLogs:
    def test_incremental_logs_after_seq(self, client):
        c, state = client
        state.log("第一条")
        state.log("第二条")
        j1 = c.get("/api/status").get_json()
        assert len(j1["logs"]) == 2
        last = j1["logs"][-1][0]
        state.log("第三条")
        j2 = c.get(f"/api/status?after={last}").get_json()
        assert [m for _s, _t, m in j2["logs"]] == ["第三条"]
