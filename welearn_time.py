import threading
from textwrap import dedent
from typing import Any, Callable, Dict, Optional, Tuple

import welearn_common
from welearn_common import LoginError, WelearnError
from welearn_study import StudyTask

REQUEST_INTERVAL = 2


class TimeTask(StudyTask):
    """刷学习时长任务控制器(CLI 与 GUI 共用)。所有输出经 on_log 回调,可在工作线程中调用。"""

    def __init__(self, username: str, password: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None,
                 session: Optional[Any] = None,
                 request_interval: float = REQUEST_INTERVAL,
                 section_gap_seconds: Tuple[int, int] = (0, 0)):
        super().__init__(
            username,
            password,
            on_log=on_log,
            stop_event=stop_event,
            session=session,
            request_interval=request_interval,
        )
        self.section_gap_seconds = section_gap_seconds

    def _generate_learning_time(self, target_time) -> int:
        if type(target_time) is int:
            return target_time
        return self._sample_span(target_time)

    def run(self, units, param, sections=None) -> Dict[str, Any]:
        """units: 0=全部单元, [n,...]=指定单元; sections: None=整单元,
        {单元编号: [小节编号]}=只刷勾选小节。"""
        result = self.run_study(
            units,
            duration=param,
            accuracy=None,
            sections=sections,
            section_gap=self.section_gap_seconds,
            skip_completed=False,
            probe_all_units=False,
        )
        return {
            "total": result["total"],
            "succeed": result["succeed"],
            "failed": result["failed"],
            "errors": result["errors"],
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
            模式1:每个练习增加指定学习时长,请直接输入时间
            如:希望每个练习增加30秒,则输入 30

            模式2:每个练习增加随机时长,请输入时间上下限并用英文逗号隔开
            如:希望每个练习增加10~30秒,则输入 10,30
        '''))
        print("\n\n")

        input_ = input("请严格按照以上格式输入: ")
        try:
            target_time = welearn_common.parse_numeric_param(input_)
        except ValueError:
            print("格式异常")
            exit(0)

        result = task.run(unit_index, target_time)

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
