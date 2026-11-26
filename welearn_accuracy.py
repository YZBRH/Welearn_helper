import threading
from typing import Any, Callable, Dict, Optional, Tuple

from welearn_common import LoginError, WelearnError
from welearn_common import parse_numeric_param
from welearn_study import DEFAULT_SECTION_GAP_SECONDS, StudyTask

DEFAULT_STUDY_SECONDS = (20, 40)


class AccuracyTask(StudyTask):
    """刷完成度正确率任务控制器(CLI 与 GUI 共用)。所有输出经 on_log 回调,可在工作线程中调用。"""

    def __init__(self, username: str, password: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None,
                 session: Optional[Any] = None,
                 study_seconds: Tuple[int, int] = DEFAULT_STUDY_SECONDS,
                 section_gap_seconds: Tuple[int, int] = DEFAULT_SECTION_GAP_SECONDS,
                 request_interval: float = 0.5):
        super().__init__(
            username,
            password,
            on_log=on_log,
            stop_event=stop_event,
            session=session,
            request_interval=request_interval,
        )
        self.study_seconds = study_seconds
        self.section_gap_seconds = section_gap_seconds

    def run(self, units, param, sections=None) -> Dict[str, Any]:
        """units: 0=全部单元(沿用原版错误驱动推进), [n,...]=指定单元;
        sections: None=整单元, {单元编号: [小节编号]}=只刷勾选小节。"""
        result = self.run_study(
            units,
            duration=self.study_seconds,
            accuracy=param,
            sections=sections,
            section_gap=self.section_gap_seconds,
            skip_completed=True,
            probe_all_units=True,
        )
        return {
            "way1_succeed": result["way1_succeed"],
            "way1_failed": result["way1_failed"],
            "way2_succeed": result["way2_succeed"],
            "way2_failed": result["way2_failed"],
        }


def _printline():
    print('-' * 51)


def welearn_accuracy_run():
    user = input("请输入用户名=>")
    pwd = input("请输入密码=>")
    task = AccuracyTask(user, pwd)
    try:
        task.login()
    except (LoginError, WelearnError) as e:
        print(e)
        exit(0)
    _printline()
    while True:
        try:
            courses = task.fetch_courses()
        except WelearnError as e:
            input(str(e))
            exit(0)
        print('查询课程成功!!!')
        _printline()
        print('我的课程: \n')

        for i, course in enumerate(courses, start=1):
            print(f'[NO.{i:>2}] 完成度{course["per"]:>3}% {course["name"]}')

        order = int(input("\n请输入需要完成的课程序号(上方[]内的数字): "))
        cid = courses[order - 1]["cid"]
        _printline()
        print("获取单元中...")
        _printline()

        units = task.fetch_units(cid)

        print('[NO. 0]  按顺序完成全部单元课程')
        for i, x in enumerate(units, start=1):
            if x['visible'] == 'true':
                print(f'[NO.{i:>2d}]  [已开放]  {x["unitname"]}  {x["name"]}')
            else:
                print(f'[NO.{i:>2d}] ![未开放]! {x["unitname"]}  {x["name"]}')
        unitidx = int(input('\n\n请选择需要完成的单元序号(上方[]内的数字,输入0为按顺序刷全部单元): '))
        _printline()
        inputcrate = input(
            '模式1:每个练习指定正确率,请直接输入指定的正确率\n如:希望每个练习正确率均为100,则输入 100\n\n模式2:每个练习随机正确率,请输入正确率上下限并用英文逗号隔开\n如:希望每个练习正确率为70~100,则输入 70,100\n\n请严格按照以上格式输入每个练习的正确率: ')
        try:
            mycrate = parse_numeric_param(inputcrate)
        except ValueError:
            print("格式异常")
            exit(0)
        _printline()

        result = task.run(unitidx, mycrate)

        if unitidx == 0:
            break
        print('本单元运行完毕!回到选课处!!\n\n\n\n')
        _printline()

    _printline()
    print(f"""
    ***************************************************
    全部完成!!

    总计:
    方式1: {result['way1_succeed']} 成功, {result['way1_failed']} 失败
    方式2: {result['way2_succeed']} 成功, {result['way2_failed']} 失败

    https://github.com/Avenshy/WELearnToSleep
    本软件遵守GPLv3协议,且免费、开源,禁止售卖!
    本软件遵守GPLv3协议,且免费、开源,禁止售卖!
    本软件遵守GPLv3协议,且免费、开源,禁止售卖!
    **********  Created By Avenshy & SSmJaE  **********""")
    input("Press any key to exit...")
