import asyncio
import json
import random
import threading
import time
from textwrap import dedent
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import welearn_common
from welearn_common import LoginError, WelearnError

REQUEST_INTERVAL = 2
HEARTBEAT_INTERVAL = 1
AJAX_URL = "https://welearn.sflep.com/Ajax/SCO.aspx"


def _default_log(msg):
    print(msg)


class TimeTask:
    """刷学习时长任务控制器(CLI 与 GUI 共用)。所有输出经 on_log 回调,可在工作线程中调用。"""

    def __init__(self, username: str, password: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None):
        self.username = username
        self.password = password
        self.on_log = on_log or _default_log
        self.stop_event = stop_event or threading.Event()
        self.session = welearn_common.new_session()
        self.cid = ''
        self.uid = ''
        self.classid = ''
        self.units: List[Any] = []
        self.request_interval = REQUEST_INTERVAL

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def login(self) -> None:
        welearn_common.login(self.session, self.username, self.password,
                             on_log=self.on_log, stop_event=self.stop_event)

    def fetch_courses(self) -> List[dict]:
        return welearn_common.fetch_courses(self.session)

    def fetch_units(self, cid) -> List[dict]:
        self.cid = str(cid)
        # 与原实现一致:先请求 /2019/ 页面(响应弃用,保持请求序列),再请求解析 uid/classid 的页面
        self.session.get(
            'https://welearn.sflep.com/2019/student/course_info.aspx?cid=' + self.cid,
            headers={'Referer': 'https://welearn.sflep.com/2019/student/index.aspx'})
        self.uid, self.classid = welearn_common.get_course_ids(self.session, self.cid)
        self.units = welearn_common.fetch_units(self.session, self.cid, self.uid)
        return self.units

    def fetch_sections(self, unit_index: int) -> List[dict]:
        """只读获取指定单元(1-based)的小节列表,供界面展示"""
        return welearn_common.fetch_sections(self.session, self.cid, self.uid,
                                             unit_index - 1, self.classid)

    def _generate_learning_time(self, target_time) -> int:
        if type(target_time) is int:
            return target_time
        return random.randint(target_time[0], target_time[1])

    def run(self, unit_index: int, param) -> Dict[str, Any]:
        target_time = param
        errors: List[str] = []
        max_learning_time = 0

        if unit_index == 0:
            start_index, end_index = 0, len(self.units)
        else:
            start_index, end_index = unit_index - 1, unit_index

        pending: List[Tuple[int, dict]] = []
        for unit in range(start_index, end_index):
            if self._stopped():
                break
            response = self.session.get(
                f"https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={self.cid}&uid={self.uid}&unitidx={str(unit)}&classid={self.classid}",
                headers={'Referer': 'https://welearn.sflep.com/2019/student/course_info.aspx?cid=' + self.cid})
            for chapter in response.json()['info']:
                learning_time = self._generate_learning_time(target_time)
                if learning_time > max_learning_time:
                    max_learning_time = learning_time
                pending.append((learning_time, chapter))

        async def runner():
            coros = [self._simulate(learning_time, chapter, errors)
                     for learning_time, chapter in pending]
            await asyncio.gather(self._heartbeat(max_learning_time), *coros)

        asyncio.run(runner())
        return {'total': len(pending), 'succeed': len(pending) - len(errors),
                'failed': len(errors), 'errors': list(errors)}

    async def _simulate(self, learning_time: int, chapter: Dict, errors: List[str]) -> None:
        self.on_log(f"章节 : {chapter['location']}")
        self.on_log(f"已学 : {chapter['learntime']} 将学 : {learning_time}")

        common_headers = {'Referer': 'https://welearn.sflep.com/student/StudyCourse.aspx'}
        common_data = {'uid': self.uid, 'cid': self.cid, 'scoid': chapter['id']}

        await asyncio.sleep(self.request_interval)
        response = self.session.post(
            AJAX_URL, data={**common_data, 'action': 'getscoinfo_v7'}, headers=common_headers)

        if '学习数据不正确' in response.text:  # 重试
            await asyncio.sleep(self.request_interval)
            self.session.post(
                AJAX_URL, data={**common_data, 'action': 'startsco160928'}, headers=common_headers)
            response = self.session.post(
                AJAX_URL, data={**common_data, 'action': 'getscoinfo_v7'}, headers=common_headers)
            if '学习数据不正确' in response.text:
                self.on_log(f"错误: {chapter['location']}")
                errors.append(chapter['location'])
                return

        return_json = response.json()['comment']
        if 'cmi' in return_json:
            cmi = json.loads(return_json)['cmi']
            crate = cmi['score']['scaled']
            cstatus = cmi['completion_status']
            progress = cmi['progress_measure']
            total_time = cmi['total_time']
            session_time = cmi['session_time']
        else:
            crate = ''
            cstatus = 'not_attempted'
            progress = '0'
            total_time = '0'
            session_time = '0'

        await asyncio.sleep(self.request_interval)
        self.session.post(
            AJAX_URL,
            data={**common_data, 'action': 'keepsco_with_getticket_with_updatecmitime',
                  'session_time': session_time, 'total_time': total_time},
            headers=common_headers)

        for current_time in range(1, learning_time + 1):
            if self._stopped():
                self.on_log(f"任务已停止,跳过剩余时长: {chapter['location']}")
                return
            await asyncio.sleep(1)
            if current_time % 60 == 0:
                self.session.post(
                    AJAX_URL,
                    data={**common_data, 'action': 'keepsco_with_getticket_with_updatecmitime',
                          'session_time': session_time, 'total_time': total_time},
                    headers=common_headers)

        await asyncio.sleep(self.request_interval)
        self.session.post(
            AJAX_URL,
            data={**common_data, 'action': 'savescoinfo160928',
                  'crate': crate, 'cstatus': cstatus, 'status': 'unknown',
                  'progress': progress, 'trycount': '0'},
            headers=common_headers)

    async def _heartbeat(self, max_learning_time: int) -> None:
        start_time = time.time()
        estimate = max_learning_time + 4 * self.request_interval
        next_report = 10
        for _ in range(estimate):
            if self._stopped():
                return
            if int(time.time() - start_time) >= next_report:
                self.on_log(f"预计学习时长 : {estimate} 已学习时长 : {int(time.time() - start_time)}")
                next_report += 10
            await asyncio.sleep(HEARTBEAT_INTERVAL)


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
