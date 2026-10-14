import threading
from random import randint
from typing import Any, Callable, Dict, List, Optional

import welearn_common
from welearn_common import LoginError, WelearnError

AJAX_URL = "https://welearn.sflep.com/Ajax/SCO.aspx"

# 伪造的完成数据报文(与原实现逐字一致,{crate} 为占位)
SCO_DATA_TEMPLATE = ('{"cmi":{"completion_status":"completed","interactions":[],"launch_data":"",'
                     '"progress_measure":"1","score":{"scaled":"{crate}","raw":"100"},'
                     '"session_time":"0","success_status":"unknown","total_time":"0","mode":"normal"},'
                     '"adl":{"data":[]},"cci":{"data":[],"service":{"dictionary":{"headword":"",'
                     '"short_cuts":""},"new_words":[],"notes":[],"writing_marking":[],"record":'
                     '{"files":[]},"play":{"offline_media_id":"9999"}},"retry_count":"0",'
                     '"submit_time":""}}[INTERACTIONINFO]')


def _default_log(msg):
    print(msg)


class AccuracyTask:
    """刷完成度正确率任务控制器(CLI 与 GUI 共用)。所有输出经 on_log 回调,可在工作线程中调用。"""

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

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def login(self) -> None:
        welearn_common.login(self.session, self.username, self.password,
                             on_log=self.on_log, stop_event=self.stop_event)

    def fetch_courses(self) -> List[dict]:
        return welearn_common.fetch_courses(self.session)

    def fetch_units(self, cid) -> List[dict]:
        self.cid = str(cid)
        self.uid, self.classid = welearn_common.get_course_ids(self.session, self.cid)
        self.units = welearn_common.fetch_units(self.session, self.cid, self.uid)
        return self.units

    def fetch_sections(self, unit_index: int) -> List[dict]:
        """只读获取指定单元(1-based)的小节列表,供界面展示"""
        return welearn_common.fetch_sections(self.session, self.cid, self.uid,
                                             unit_index - 1, self.classid)

    def run(self, unit_index: int, param) -> Dict[str, Any]:
        random_mode = isinstance(param, tuple)
        way1_succeed, way2_succeed = 0, 0
        way1_failed, way2_failed = 0, 0

        info_headers = {
            "Referer": f"https://welearn.sflep.com/2019/student/course_info.aspx?cid={self.cid}",
        }

        if unit_index == 0:
            unit = 0
            advance_until_error = True   # 原版语义:持续探测下一单元,直到服务器报错才终止
        else:
            unit = unit_index - 1
            advance_until_error = False

        while True:
            if self._stopped():
                break
            response = self.session.get(
                f'https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={self.cid}&uid={self.uid}&unitidx={unit}&classid={self.classid}',
                headers=info_headers)

            if "异常" in response.text or "出错了" in response.text:
                break

            for course in response.json()["info"]:
                if self._stopped():
                    break
                if course['isvisible'] == 'false':  # 跳过未开放课程
                    self.on_log(f'[!!跳过!!]    {course["location"]}')
                elif "未" in course["iscomplete"]:  # 章节未完成
                    self.on_log(f'[即将完成]    {course["location"]}')
                    if random_mode:
                        crate = str(randint(param[0], param[1]))
                    else:
                        crate = str(param)
                    data = SCO_DATA_TEMPLATE.replace('{crate}', crate)
                    sco_headers = {
                        "Referer": f"https://welearn.sflep.com/Student/StudyCourse.aspx?cid={self.cid}&classid={self.classid}&sco={course['id']}"}

                    self.session.post(AJAX_URL, data={"action": "startsco160928",
                                                      "cid": self.cid,
                                                      "scoid": course["id"],
                                                      "uid": self.uid},
                                      headers=sco_headers)
                    response = self.session.post(AJAX_URL, data={"action": "setscoinfo",
                                                                 "cid": self.cid,
                                                                 "scoid": course["id"],
                                                                 "uid": self.uid,
                                                                 "data": data,
                                                                 "isend": "False"},
                                                 headers=sco_headers)
                    way1_ok = '"ret":0' in response.text
                    if way1_ok:
                        way1_succeed += 1
                    else:
                        way1_failed += 1

                    response = self.session.post(AJAX_URL, data={"action": "savescoinfo160928",
                                                                 "cid": self.cid,
                                                                 "scoid": course["id"],
                                                                 "uid": self.uid,
                                                                 "progress": "100",
                                                                 "crate": crate,
                                                                 "status": "unknown",
                                                                 "cstatus": "completed",
                                                                 "trycount": "0",
                                                                 },
                                                 headers=sco_headers)
                    way2_ok = '"ret":0' in response.text
                    if way2_ok:
                        way2_succeed += 1
                    else:
                        way2_failed += 1
                    self.on_log(f'>>>>>>>>>>>>>>正确率:{crate:>3}%  '
                                f'方式1:{"成功!!!" if way1_ok else "失败!!!"}  '
                                f'方式2:{"成功!!!" if way2_ok else "失败!!!"}')
                else:  # 章节已完成
                    self.on_log(f'[ 已完成 ]    {course["location"]}')

            if not advance_until_error:
                break
            unit += 1

        return {'way1_succeed': way1_succeed, 'way1_failed': way1_failed,
                'way2_succeed': way2_succeed, 'way2_failed': way2_failed}


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
            mycrate = welearn_common.parse_numeric_param(inputcrate)
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
