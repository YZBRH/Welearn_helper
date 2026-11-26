import threading
from textwrap import dedent
from typing import Any, Callable, Dict, Optional, Tuple

import welearn_common
from welearn_common import LoginError, WelearnError
from welearn_study import (
    DEFAULT_SECTION_GAP_SECONDS,
    DEFAULT_STUDY_SECONDS,
    MAX_PARALLEL_WORKERS,
    StudyTask,
)

REQUEST_INTERVAL = 2


class TimeTask(StudyTask):
    """刷学习时长任务控制器(CLI 与 GUI 共用)。所有输出经 on_log 回调,可在工作线程中调用。"""

    def __init__(self, username: str, password: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None,
                 session: Optional[Any] = None,
                 request_interval: float = REQUEST_INTERVAL,
                 section_gap_seconds: Tuple[int, int] = (0, 0),
                 accuracy_section_gap_seconds: Tuple[int, int] =
                 DEFAULT_SECTION_GAP_SECONDS):
        super().__init__(
            username,
            password,
            on_log=on_log,
            stop_event=stop_event,
            session=session,
            request_interval=request_interval,
        )
        self.section_gap_seconds = section_gap_seconds
        self.accuracy_section_gap_seconds = accuracy_section_gap_seconds

    def _generate_learning_time(self, target_time) -> int:
        if type(target_time) is int:
            return target_time
        return self._sample_span(target_time)

    def run(self, units, param=None, sections=None,
            accuracy=None, parallel_workers: int = 1) -> Dict[str, Any]:
        """units: 0=全部单元, [n,...]=指定单元; sections: None=整单元,
        {单元编号: [小节编号]}=只刷勾选小节。

        param 为每项学习时长;留空且指定 accuracy 时沿用原正确率模式的
        20~40 秒默认学习时长。accuracy 留空则只刷时长并保留原正确率。
        parallel_workers 控制同时计时的小节数;网络请求仍串行提交。
        """
        duration = DEFAULT_STUDY_SECONDS if param is None else param
        has_accuracy = accuracy is not None
        accuracy_only = param is None and has_accuracy
        result = self.run_study(
            units,
            duration=duration,
            accuracy=accuracy,
            sections=sections,
            section_gap=(self.accuracy_section_gap_seconds
                         if accuracy_only else self.section_gap_seconds),
            skip_completed=accuracy_only,
            probe_all_units=accuracy_only,
            parallel_workers=parallel_workers,
        )
        return {
            "total": result["total"],
            "succeed": result["succeed"],
            "failed": result["failed"],
            "errors": result["errors"],
            "accuracy_submitted": result["accuracy_submitted"],
            "way1_succeed": result["way1_succeed"],
            "way1_failed": result["way1_failed"],
            "way2_succeed": result["way2_succeed"],
            "way2_failed": result["way2_failed"],
        }


def welearn_time_run():
    user = input("请输入用户名=>")
    pwd = input("请输入密码=>")
    task = TimeTask(user, pwd)
    try:
        task.login()
    except (LoginError, WelearnError) as e:
        print(e)
        exit(0)
    while True:
        try:
            courses = task.fetch_courses()
        except WelearnError as e:
            input(str(e))
            exit(0)
        for index, course in enumerate(courses, start=1):
            print('[id:{:>2d}]  完成度 {:>3d}%  {}'.format(index, course['per'], course['name']))

        index = int(input('\n请输入需要刷时长的课程id(id为上方[]内的序号): '))
        units = task.fetch_units(courses[index - 1]['cid'])

        print("\n\n")
        print('[id: 0]  按顺序刷全部单元学习时长')
        for index, unit in enumerate(units, start=1):
            print(f"[id:{index:>2d}]  {unit['unitname']}  {unit['name']}")
        print("\n\n")
        unit_index = int(input('请选择要刷时长的单元id(id为上方[]内的序号,输入0为刷全部单元): '))

        print("\n\n")
        print(dedent('''\
            每个练习先学习指定时长:
            - 固定时长直接输入数字,如 30
            - 随机时长输入英文逗号分隔的上下限,如 10,30
            - 直接回车沿用原正确率模式的默认时长 20,40
        '''))
        print("\n\n")

        input_ = input("请输入每项学习时长: ").strip()
        try:
            target_time = (
                welearn_common.parse_numeric_param(input_) if input_ else None
            )
        except ValueError:
            print("格式异常")
            exit(0)

        input_accuracy = input(
            "完成后写入正确率(100 或 70,100;直接回车保留原正确率): "
        ).strip()
        try:
            accuracy = (
                welearn_common.parse_numeric_param(input_accuracy)
                if input_accuracy else None
            )
        except ValueError:
            print("格式异常")
            exit(0)

        workers_input = input(
            f"并发计时小节数(1~{MAX_PARALLEL_WORKERS},直接回车=1): "
        ).strip()
        try:
            parallel_workers = int(workers_input) if workers_input else 1
            if not 1 <= parallel_workers <= MAX_PARALLEL_WORKERS:
                raise ValueError
        except ValueError:
            print(f"并发小节数必须在 1~{MAX_PARALLEL_WORKERS} 之间")
            exit(0)

        result = task.run(
            unit_index,
            target_time,
            accuracy=accuracy,
            parallel_workers=parallel_workers,
        )

        print('运行结束!!\n错误:', result['failed'], '个')
        for index, error in enumerate(result['errors'], start=1):
            print(f'第{index}个错误:{error}')

        if unit_index == 0:
            break
        print("\n\n")
        print('回到选课处!!')
        print("\n\n")

    print("\n\n")
    print(dedent('''\
        **********  Created By Avenshy & SSmJaE  **********
                        Version : 0.3.0
           基于GPL3.0,完全开源,免费,禁止二次倒卖或商用
           https://www.github.com/Avenshy/WELearnToSleeep
                        仅供学习,勿滥用
        ***************************************************
    '''))
    input("Press any key to exit...")
