import threading

import pytest

from tests.fakes import FakeResponse, FakeSession
import welearn_common
from welearn_common import LoginError, WelearnError

# 顺序经过 split("&") 后 index 4 是 code_challenge、index 6 是 state
LOC = ("https://sso.sflep.com/idsvr/connect/authorize?client_id=welearn_web"
       "&redirect_uri=https%3A%2F%2Fwelearn.sflep.com%2Fsignin-sflep"
       "&response_type=code&scope=openid&code_challenge=TEST_CHALLENGE"
       "&code_challenge_method=S256&state=TEST_STATE&x-client-SKU=ID_NET472")


def make_login_session(code=0):
    fake = FakeSession()
    fake.routes["https://welearn.sflep.com/user/prelogin.aspx"] = \
        FakeResponse(headers={"Location": LOC})
    fake.routes["https://sso.sflep.com/idsvr/account/login"] = \
        FakeResponse(json_data={"code": code, "data": "/next"})
    fake.routes["https://sso.sflep.com/idsvr/next"] = FakeResponse(text="ok")
    return fake


class TestLogin:
    def test_success(self):
        fake = make_login_session(code=0)
        logs = []
        welearn_common.login(fake, "user", "pw", on_log=logs.append)
        assert any("登录成功" in m for m in logs)
        assert any(m[0] == "POST" and "account/login" in m[1] for m in fake.requests)

    def test_wrong_password_raises_login_error(self):
        fake = make_login_session(code=1)
        with pytest.raises(LoginError):
            welearn_common.login(fake, "user", "bad")

    def test_stop_event_interrupts_retry_loop(self):
        fake = make_login_session(code=-1)   # code=-1 会一直重试
        stop = threading.Event()
        stop.set()
        with pytest.raises(WelearnError):
            welearn_common.login(fake, "user", "pw", stop_event=stop)


class TestFetchCourses:
    def test_returns_clist(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/ajax/authCourse.aspx"] = FakeResponse(
            json_data={"clist": [{"cid": 11, "per": 60, "name": "课程A"}]})
        courses = welearn_common.fetch_courses(fake)
        assert courses == [{"cid": 11, "per": 60, "name": "课程A"}]

    def test_empty_clist_raises(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/ajax/authCourse.aspx"] = FakeResponse(
            json_data={"clist": []})
        with pytest.raises(WelearnError):
            welearn_common.fetch_courses(fake)


class TestGetCourseIds:
    def test_extracts_uid_and_classid(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/student/course_info.aspx"] = FakeResponse(
            text='var x = {"uid":12345,"name":"n"}; var y = {"classid":"C678"};')
        assert welearn_common.get_course_ids(fake, "99") == ("12345", "C678")


class TestFetchUnits:
    def test_returns_info_list(self):
        fake = FakeSession()
        fake.routes["https://welearn.sflep.com/ajax/StudyStat.aspx"] = FakeResponse(
            json_data={"info": [{"unitname": "U1", "name": "第一单元", "visible": "true"}]})
        units = welearn_common.fetch_units(fake, "1", "2")
        assert units[0]["unitname"] == "U1"
