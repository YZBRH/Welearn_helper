import time

import pytest

import web_gui
import welearn_accuracy
import welearn_common


class FakeTask:
    """替代 AccuracyTask:复刻控制器契约"""

    instances = []

    def __init__(self, username, password, on_log=None, stop_event=None):
        self.username = username
        self.on_log = on_log
        self.stop_event = stop_event
        FakeTask.instances.append(self)

    def login(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise welearn_common.WelearnError("已停止")

    def fetch_courses(self):
        return [{"cid": 11, "per": 60, "name": "课程A"}]

    def fetch_units(self, cid):
        return [{"unitname": "U1", "name": "第一单元", "visible": "true"}]

    def run(self, unit_index, param):
        time.sleep(0.2)   # 让"任务运行中"状态可被观测,避免与断言竞态
        self.on_log(f"fake run unit={unit_index} param={param}")
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
        """切模式必须作废服务端旧 task,否则不重登就能用旧模式跑新参数"""
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
