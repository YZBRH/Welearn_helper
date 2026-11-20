import time

import pytest

import web_gui
import welearn_flow


class FakeFlowTask:
    """替代 FlowTask:复刻控制器契约(含 skip_event / progress)"""

    instances = []

    def __init__(self, username, password, on_log=None, stop_event=None,
                 session=None, skip_event=None):
        self.username = username
        self.password = password
        self.on_log = on_log
        self.stop_event = stop_event
        self.skip_event = skip_event
        self.session = session or object()
        self.cid = ""
        self.uid = ""
        self.classid = ""
        self.units = []
        self.progress = {}
        self.last_jobs = None
        FakeFlowTask.instances.append(self)

    def login(self):
        pass

    def fetch_courses(self):
        return [{"cid": 11, "per": 60, "name": "课程A"},
                {"cid": 22, "per": 10, "name": "课程B"}]

    def fetch_units(self, cid):
        self.cid = str(cid)
        return [{"unitname": "U1", "name": "第一单元", "visible": "true"}]

    def fetch_sections(self, unit_index):
        return [{"location": "U1 > A", "iscomplete": "未完成", "isvisible": "true"},
                {"location": "U1 > B", "iscomplete": "completed", "isvisible": "true"}]

    def run_queue(self, jobs, on_error="continue"):
        self.last_jobs = jobs
        time.sleep(0.2)
        self.on_log("fake queue run")
        return {"total": len(jobs), "done": 1, "skipped": 1, "failed": 0,
                "elapsed": 1.0, "stopped": False,
                "courses": [{"no": j["no"], "name": j["name"], "status": "done",
                             "done": 1, "skipped": 1, "failed": 0, "errors": []}
                            for j in jobs]}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(welearn_flow, "FlowTask", FakeFlowTask)
    FakeFlowTask.instances = []
    app, state = web_gui.create_app()
    return app.test_client(), state


def login_queue(client):
    c, _ = client
    return c.post("/api/login", json={"username": "u", "password": "p", "mode": "queue"})


class TestQueueMode:
    def test_login_accepts_queue_mode(self, client):
        j = login_queue(client).get_json()
        assert j["ok"] is True and len(j["courses"]) == 2

    def test_page_has_queue_option_and_second_param_box(self, client):
        c, _ = client
        body = c.get("/").get_data(as_text=True)
        assert 'value="queue"' in body and "顺序刷取" in body
        assert 'id="paramDur"' in body          # 学习时长输入框
        assert 'id="param"' in body             # 正确率输入框
        assert 'id="coursePick"' not in body    # 队列课程勾选列表已移除
        assert "/api/queue/start" in body

    def test_mode_switch_to_queue_reuses_session(self, client):
        c, state = client
        login_queue(client)
        old = state.task
        r = c.post("/api/mode", json={"mode": "queue"})
        assert r.get_json()["ok"] is True
        assert state.mode == "queue"
        assert state.task.session is old.session


class TestQueueStart:
    def test_jobs_sorted_by_course_number(self, client):
        c, state = client
        login_queue(client)
        j = c.post("/api/queue/start", json={"cids": ["22", "11"],
                                             "accuracy": "70,95",
                                             "duration": "40,90"}).get_json()
        assert j["ok"] is True and j["total"] == 2
        state.worker.join(timeout=5)
        jobs = FakeFlowTask.instances[-1].last_jobs
        assert [job["no"] for job in jobs] == [1, 2]      # 课程编号升序
        assert [job["cid"] for job in jobs] == ["11", "22"]
        assert jobs[0]["accuracy"] == (70, 95) and jobs[0]["duration"] == (40, 90)

    def test_missing_duration_rejected(self, client):
        c, _ = client
        login_queue(client)
        r = c.post("/api/queue/start", json={"cids": ["11"], "accuracy": "80"})
        assert r.status_code == 400
        assert "学习时长" in r.get_json()["error"]

    def test_accuracy_over_100_rejected(self, client):
        c, _ = client
        login_queue(client)
        r = c.post("/api/queue/start", json={"cids": ["11"], "accuracy": "120",
                                             "duration": "30"})
        assert r.status_code == 400
        assert "100" in r.get_json()["error"]

    def test_unknown_course_rejected(self, client):
        c, _ = client
        login_queue(client)
        r = c.post("/api/queue/start", json={"cids": ["999"], "accuracy": "80",
                                             "duration": "30"})
        assert r.status_code == 400
        assert "不在当前课程列表" in r.get_json()["error"]

    def test_empty_selection_rejected(self, client):
        c, _ = client
        login_queue(client)
        r = c.post("/api/queue/start", json={"cids": [], "accuracy": "80",
                                             "duration": "30"})
        assert r.status_code == 400

    def test_second_start_while_running_rejected(self, client):
        c, state = client
        login_queue(client)
        c.post("/api/queue/start", json={"cids": ["11"], "accuracy": "80", "duration": "1"})
        r = c.post("/api/queue/start", json={"cids": ["11"], "accuracy": "80",
                                             "duration": "1"})
        assert r.status_code == 409
        state.worker.join(timeout=5)

    def test_units_and_sections_passed_into_jobs(self, client):
        c, state = client
        login_queue(client)
        c.post("/api/units", json={"cid": 11})
        j = c.post("/api/queue/start", json={"cids": ["11"], "accuracy": "80",
                                             "duration": "1",
                                             "units": [1],
                                             "sections": {"1": [2]}}).get_json()
        assert j["ok"] is True
        state.worker.join(timeout=5)
        job = FakeFlowTask.instances[-1].last_jobs[0]
        assert job["units"] == [1]
        assert job["sections"] == {1: [2]}

    def test_default_scope_is_all_units(self, client):
        c, state = client
        login_queue(client)
        c.post("/api/queue/start", json={"cids": ["11"], "accuracy": "80", "duration": "1"})
        state.worker.join(timeout=5)
        job = FakeFlowTask.instances[-1].last_jobs[0]
        assert job["units"] == 0 and job["sections"] is None

    def test_unknown_unit_selection_rejected(self, client):
        c, _ = client
        login_queue(client)
        r = c.post("/api/queue/start", json={"cids": ["11"], "accuracy": "80",
                                             "duration": "1", "units": []})
        assert r.status_code == 400
        assert "未选择任何单元" in r.get_json()["error"]

    def test_result_logged_after_finish(self, client):
        c, state = client
        login_queue(client)
        c.post("/api/queue/start", json={"cids": ["11", "22"], "accuracy": "80",
                                         "duration": "1"})
        state.worker.join(timeout=5)
        logs = [m for _s, _t, m in c.get("/api/status").get_json()["logs"]]
        assert any("顺序任务结束" in line for line in logs)
        assert state.running is False


class TestQueueSkip:
    def test_skip_without_running_rejected(self, client):
        c, _ = client
        login_queue(client)
        assert c.post("/api/queue/skip").status_code == 409

    def test_skip_sets_event_while_running(self, client):
        c, state = client
        login_queue(client)
        c.post("/api/queue/start", json={"cids": ["11"], "accuracy": "80", "duration": "1"})
        j = c.post("/api/queue/skip").get_json()
        assert j["ok"] is True and state.skip_event.is_set()
        state.worker.join(timeout=5)


class TestQueueStatus:
    def test_status_exposes_queue_snapshot(self, client):
        c, state = client
        login_queue(client)
        c.post("/api/queue/start", json={"cids": ["11", "22"], "accuracy": "80",
                                         "duration": "1"})
        j = c.get("/api/status").get_json()
        assert j["queue"]["total"] == 2
        assert len(j["queue"]["names"]) == 2
        state.worker.join(timeout=5)

    def test_preview_counts_sections(self, client):
        c, _ = client
        login_queue(client)
        j = c.post("/api/queue/preview", json={"cids": ["11"], "accuracy": "80",
                                               "duration": "40,90"}).get_json()
        assert j["ok"] is True
        assert j["rows"][0]["sections"] == 2
        assert j["sections"] == 2
        assert j["seconds"] > 0 and j["eta"]

    def test_preview_requires_login(self, client):
        c, _ = client
        r = c.post("/api/queue/preview", json={"cids": ["11"], "accuracy": "80",
                                               "duration": "30"})
        assert r.status_code == 400
