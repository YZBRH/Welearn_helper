"""Welearn_helper 图形界面(tkinter)。刷课任务在后台线程运行,可随时停止。"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import List

import welearn_accuracy
import welearn_common
import welearn_time
from welearn_common import LoginError, WelearnError

BANNER = """**********  Created By Avenshy & SSmJaE  **********
                 图形界面Version:1.0
        原项目作者github https://github.com/Avenshy
        更新维护者github https://github.com/lxbsdsg/Welearn_helper/
***************************************************"""


class WelearnApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Welearn_helper 图形界面")
        root.geometry("980x680")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread = None
        self.task = None            # AccuracyTask 或 TimeTask
        self.courses: List[dict] = []
        self.units: List[dict] = []

        self._build_widgets()
        self._set_running(False)
        self.root.after(100, self._poll_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 界面搭建 ----------
    def _build_widgets(self):
        ttk.Label(self.root, text=BANNER, font=("Consolas", 10), justify=tk.CENTER)\
            .pack(pady=(8, 4))

        login_frame = ttk.LabelFrame(self.root, text="登录")
        login_frame.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(login_frame, text="账号:").grid(row=0, column=0, padx=4, pady=4, sticky=tk.W)
        self.username_entry = ttk.Entry(login_frame, width=16)
        self.username_entry.grid(row=0, column=1, padx=4)
        ttk.Label(login_frame, text="密码:").grid(row=0, column=2, padx=4)
        self.password_entry = ttk.Entry(login_frame, width=16, show="*")
        self.password_entry.grid(row=0, column=3, padx=4)
        self.mode_var = tk.StringVar(value="accuracy")
        ttk.Radiobutton(login_frame, text="刷完成度正确率", variable=self.mode_var,
                        value="accuracy", command=self._on_mode_changed)\
            .grid(row=0, column=4, padx=(12, 2))
        ttk.Radiobutton(login_frame, text="刷学习时长", variable=self.mode_var,
                        value="time", command=self._on_mode_changed)\
            .grid(row=0, column=5, padx=2)
        self.login_button = ttk.Button(login_frame, text="登录", command=self._on_login)
        self.login_button.grid(row=0, column=6, padx=12)

        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        course_frame = ttk.LabelFrame(list_frame, text="课程(登录后自动加载,选中后加载单元)")
        course_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.course_tree = ttk.Treeview(course_frame, columns=("per", "name"),
                                        show="headings", height=8)
        self.course_tree.heading("per", text="完成度%")
        self.course_tree.heading("name", text="课程名称")
        self.course_tree.column("per", width=70, anchor=tk.E)
        self.course_tree.column("name", width=280)
        self.course_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.course_tree.bind("<<TreeviewSelect>>", self._on_course_selected)
        self.refresh_button = ttk.Button(course_frame, text="刷新课程", command=self._on_refresh)
        self.refresh_button.pack(pady=(0, 4))

        unit_frame = ttk.LabelFrame(list_frame, text="单元(选中课程后加载,选中行自动填入单元id)")
        unit_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.unit_tree = ttk.Treeview(unit_frame, columns=("id", "unitname", "name", "visible"),
                                      show="headings", height=8)
        self.unit_tree.heading("id", text="id")
        self.unit_tree.heading("unitname", text="单元")
        self.unit_tree.heading("name", text="名称")
        self.unit_tree.heading("visible", text="开放")
        self.unit_tree.column("id", width=40, anchor=tk.E)
        self.unit_tree.column("unitname", width=120)
        self.unit_tree.column("name", width=180)
        self.unit_tree.column("visible", width=60, anchor=tk.CENTER)
        self.unit_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.unit_tree.bind("<<TreeviewSelect>>", self._on_unit_selected)

        param_frame = ttk.LabelFrame(self.root, text="任务参数")
        param_frame.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(param_frame, text="单元id(0=全部):").pack(side=tk.LEFT, padx=4)
        self.unit_entry = ttk.Entry(param_frame, width=6)
        self.unit_entry.pack(side=tk.LEFT, padx=4)
        self.unit_entry.insert(0, "0")
        self.param_label = ttk.Label(param_frame, text="正确率(100 或 70,100):")
        self.param_label.pack(side=tk.LEFT, padx=(16, 4))
        self.param_entry = ttk.Entry(param_frame, width=12)
        self.param_entry.pack(side=tk.LEFT, padx=4)

        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill=tk.X, padx=8, pady=4)
        self.start_button = ttk.Button(control_frame, text="开始", command=self._on_start)
        self.start_button.pack(side=tk.LEFT, padx=4)
        self.stop_button = ttk.Button(control_frame, text="停止", command=self._on_stop,
                                      state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=4)
        self.status_var = tk.StringVar(value="待命")
        ttk.Label(control_frame, textvariable=self.status_var).pack(side=tk.RIGHT, padx=8)

        log_frame = ttk.LabelFrame(self.root, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, state=tk.DISABLED,
                                                  font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ---------- 日志(线程安全:只入队,主线程轮询写出) ----------
    def _worker_log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.root.after(100, self._poll_log_queue)

    def _log_gui(self, msg: str):
        self.log_queue.put(msg)   # 主线程也走队列,保证顺序一致

    # ---------- 状态 ----------
    def _set_running(self, running: bool, running_what: str = None):
        off = tk.DISABLED if running else tk.NORMAL
        self.login_button.configure(state=off)
        self.start_button.configure(state=off)
        self.refresh_button.configure(state=off)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        for entry in (self.username_entry, self.password_entry,
                      self.unit_entry, self.param_entry):
            entry.configure(state='readonly' if running else 'normal')
        self.status_var.set(running_what if running_what else "待命")

    def _on_mode_changed(self):
        self.task = None
        self.courses = []
        self.units = []
        for tree in (self.course_tree, self.unit_tree):
            tree.delete(*tree.get_children())
        if self.mode_var.get() == "accuracy":
            self.param_label.configure(text="正确率(100 或 70,100):")
        else:
            self.param_label.configure(text="每练习时长秒(30 或 10,30):")
        self._log_gui("已切换模式,请重新登录")

    # ---------- 登录 ----------
    def _on_login(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            messagebox.showwarning("提示", "请输入账号和密码")
            return
        self._set_running(True, "登录中...")
        self.stop_event.clear()   # 此前点过"停止"不得阻塞新的登录
        mode = self.mode_var.get()

        def work():
            cls = welearn_accuracy.AccuracyTask if mode == "accuracy" else welearn_time.TimeTask
            task = cls(username, password, on_log=self._worker_log,
                       stop_event=self.stop_event)
            try:
                task.login()
                courses = task.fetch_courses()
                self.root.after(0, lambda: self._after_login(True, task, courses, None))
            except (LoginError, WelearnError) as e:
                self.root.after(0, lambda: self._after_login(False, None, None, str(e)))
            except Exception as e:
                self.root.after(0, lambda: self._after_login(False, None, None, repr(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _after_login(self, ok, task, courses, error):
        self._set_running(False)
        if not ok:
            messagebox.showerror("登录失败", error)
            return
        self.task = task
        self.courses = courses
        self._fill_courses()
        self._log_gui(f"共 {len(courses)} 门课程,请在左侧选择课程")

    def _fill_courses(self):
        self.course_tree.delete(*self.course_tree.get_children())
        for i, course in enumerate(self.courses, start=1):
            self.course_tree.insert("", tk.END, iid=str(i),
                                    values=(course.get("per", 0), course.get("name", "")))

    def _on_refresh(self):
        if self.task is None:
            messagebox.showwarning("提示", "请先登录")
            return
        self._set_running(True, "刷新课程中...")
        self.stop_event.clear()

        def work():
            try:
                courses = self.task.fetch_courses()
                self.root.after(0, lambda: self._after_refresh(courses, None))
            except (LoginError, WelearnError) as e:
                self.root.after(0, lambda: self._after_refresh(None, str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _after_refresh(self, courses, error):
        self._set_running(False)
        if error is not None:
            messagebox.showerror("刷新失败", error)
            return
        self.courses = courses
        self._fill_courses()
        self._log_gui(f"已刷新,共 {len(courses)} 门课程")

    # ---------- 课程 → 单元 ----------
    def _selected_course(self):
        selection = self.course_tree.selection()
        if not selection:
            return None
        return self.courses[int(selection[0]) - 1]

    def _on_course_selected(self, _event):
        course = self._selected_course()
        if course is None or self.task is None:
            return
        if self.worker is not None and self.worker.is_alive():
            return
        cid = course["cid"]
        self._set_running(True, "加载单元中...")
        self.stop_event.clear()

        def work():
            try:
                units = self.task.fetch_units(cid)
                self.root.after(0, lambda: self._after_units(units, None))
            except (LoginError, WelearnError) as e:
                self.root.after(0, lambda: self._after_units(None, str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _after_units(self, units, error):
        self._set_running(False)
        if error is not None:
            messagebox.showerror("加载单元失败", error)
            return
        self.units = units
        self.unit_tree.delete(*self.unit_tree.get_children())
        self.unit_tree.insert("", tk.END, iid="0", values=(0, "全部单元", "按顺序刷全部", ""))
        for i, unit in enumerate(self.units, start=1):
            visible = "是" if str(unit.get("visible", "true")).lower() == "true" else "否"
            self.unit_tree.insert("", tk.END, iid=str(i),
                                  values=(i, unit.get("unitname", ""), unit.get("name", ""),
                                          visible))
        self._log_gui(f"已加载 {len(self.units)} 个单元,选中行自动填入单元id")

    def _on_unit_selected(self, _event):
        selection = self.unit_tree.selection()
        if selection:
            self.unit_entry.delete(0, tk.END)
            self.unit_entry.insert(0, selection[0])

    # ---------- 任务 ----------
    def _on_start(self):
        if self.task is None:
            messagebox.showwarning("提示", "请先登录")
            return
        if not self.units:
            messagebox.showwarning("提示", "请先选择课程加载单元")
            return
        try:
            unit_index = int(self.unit_entry.get())
        except ValueError:
            messagebox.showwarning("提示", "单元id必须是数字(0=全部)")
            return
        try:
            param = welearn_common.parse_numeric_param(self.param_entry.get())
        except ValueError as e:
            messagebox.showwarning("提示", f"参数格式错误:{e}\n模式1: 100   模式2: 70,100")
            return
        self.stop_event.clear()
        self._set_running(True, "任务运行中")
        self._log_gui(f"开始任务:单元id={unit_index} 参数={param}")

        def work():
            try:
                result = self.task.run(unit_index, param)
                self.root.after(0, lambda: self._after_run(True, result, None))
            except Exception as e:
                self.root.after(0, lambda: self._after_run(False, None, repr(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _after_run(self, ok, result, error):
        self._set_running(False)
        if not ok:
            self._log_gui(f"任务异常终止:{error}")
            return
        if self.mode_var.get() == "time":
            self._log_gui(f"运行结束!!错误: {result['failed']} 个")
            for i, err in enumerate(result["errors"], start=1):
                self._log_gui(f"第{i}个错误:{err}")
        else:
            self._log_gui("方式1: {} 成功, {} 失败".format(result["way1_succeed"],
                                                         result["way1_failed"]))
            self._log_gui("方式2: {} 成功, {} 失败".format(result["way2_succeed"],
                                                         result["way2_failed"]))

    def _on_stop(self):
        self.stop_event.set()
        self._log_gui("已请求停止,等待任务收尾...")

    def _on_close(self):
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askokcancel("退出", "任务正在运行,确定停止并退出吗?"):
                return
            self.stop_event.set()
            self.worker.join(timeout=5)
        self.root.destroy()


def main():
    root = tk.Tk()
    WelearnApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
