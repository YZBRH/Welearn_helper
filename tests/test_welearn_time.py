import json
import threading
import time

from tests.fakes import FakeResponse, FakeSession
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

    def test_bad_sco_recorded_as_error(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = \
            FakeResponse(text="学习数据不正确")
        chapters = [{"id": 1, "location": "BadChapter", "learntime": 0}]
        task = make_task(fake, chapters)
        result = task.run(1, 1)
        assert result["failed"] == 1
        assert result["errors"] == ["BadChapter"]

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
