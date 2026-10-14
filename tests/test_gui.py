import tkinter as tk

import gui
import welearn_accuracy
import welearn_common


class FakeTask:
    """替代 AccuracyTask:复刻控制器契约——stop_event 置位时 login 抛 WelearnError"""

    login_ok = None   # 类属性,记录最近一次 login 是否在事件已清除的状态下执行

    def __init__(self, username, password, on_log=None, stop_event=None):
        self.username = username
        self.stop_event = stop_event

    def login(self):
        FakeTask.login_ok = not (self.stop_event is not None and self.stop_event.is_set())
        if not FakeTask.login_ok:
            raise welearn_common.WelearnError("已停止")

    def fetch_courses(self):
        return [{"cid": 1, "per": 10, "name": "课程A"}]


def make_app(monkeypatch):
    monkeypatch.setattr(welearn_accuracy, "AccuracyTask", FakeTask)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: None)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: None)
    FakeTask.login_ok = None
    root = tk.Tk()
    root.withdraw()
    app = gui.WelearnApp(root)
    app.username_entry.insert(0, "user")
    app.password_entry.insert(0, "pass")
    # 无 mainloop 时工作线程调 root.after 会 RuntimeError;改为记录、事后在主线程手动执行
    scheduled = []

    def record_after(ms, func=None, *args):
        if func is not None:
            scheduled.append((func, args))
        return "id"

    monkeypatch.setattr(app.root, "after", record_after)
    return root, app, scheduled


class TestReloginAfterStop:
    def test_login_after_stop_succeeds(self, monkeypatch):
        root, app, scheduled = make_app(monkeypatch)
        try:
            app.stop_event.set()          # 模拟此前点过"停止"
            app._on_login()
            app.worker.join(timeout=5)
            assert FakeTask.login_ok is True, "停止事件未清除导致登录被拒绝"
            for func, args in list(scheduled):
                func(*args)               # 主线程执行排队的回调(_after_login 等)
            assert app.task is not None
        finally:
            root.destroy()


class RaisingFakeTask(FakeTask):
    def login(self):
        raise welearn_common.LoginError("帐号或密码错误!")


class TestLoginFailurePath:
    def test_failure_callback_reports_error_without_nameerror(self, monkeypatch):
        """except 块结束会自动删除 as e;lambda 晚于块执行会 NameError(用户实测踩中)"""
        monkeypatch.setattr(welearn_accuracy, "AccuracyTask", RaisingFakeTask)
        shown = []
        monkeypatch.setattr(gui.messagebox, "showerror",
                            lambda title, msg, **k: shown.append(msg))
        root = tk.Tk()
        root.withdraw()
        app = gui.WelearnApp(root)
        app.username_entry.insert(0, "user")
        app.password_entry.insert(0, "pass")
        scheduled = []
        monkeypatch.setattr(app.root, "after",
                            lambda ms, func=None, *a: (scheduled.append((func, a)), "id")[1])
        try:
            app._on_login()
            app.worker.join(timeout=5)
            for func, args in list(scheduled):
                func(*args)               # 此处曾抛 NameError: cannot access free variable 'e'
            assert app.task is None
            assert shown == ["帐号或密码错误!"]
        finally:
            root.destroy()
