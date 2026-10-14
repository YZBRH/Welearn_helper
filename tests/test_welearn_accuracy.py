from tests.fakes import FakeResponse, FakeSession
import welearn_accuracy


def make_task(fake, chapters):
    task = welearn_accuracy.AccuracyTask("u", "p", on_log=lambda m: None)
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
        sco_posts = [r for r in fake.requests if r[1] == "https://welearn.sflep.com/Ajax/SCO.aspx"]
        assert len(sco_posts) == 3   # 仅未完成章节: startsco + setsco + savesco

    def test_failure_counters(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/Ajax/SCO.aspx"] = FakeResponse(text='{"ret":1}')
        chapters = [{"id": 1, "location": "L1", "isvisible": "true", "iscomplete": "未完成"}]
        task = make_task(fake, chapters)
        result = task.run(1, (70, 100))
        assert result["way1_failed"] == 1
        assert result["way2_failed"] == 1
        data = fake.requests[2][2]   # setscoinfo 的 data(requests[2],预检裁决)
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
        task = welearn_accuracy.AccuracyTask("u", "p", on_log=lambda m: None)
        task.session = fake
        task.cid, task.uid, task.classid = "2833", "9", "K"
        info = task.fetch_sections(3)
        assert info[0]["location"] == "L1"
        assert "unitidx=2" in fake.requests[0][1]
