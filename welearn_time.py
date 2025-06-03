import asyncio
import json
import random
import re
import time
from textwrap import dedent
from typing import Any, Dict, List, Union
from bs4 import BeautifulSoup
import base64

import requests

REQUEST_INTERVAL = 2
HEARTBEAT_INTERVAL = 1
AJAX_URL = "https://welearn.sflep.com/Ajax/SCO.aspx"

cid: str
uid: str
classid: str
courseInfo: List[Any]
unitIndex: int
targetTime: Union['int', List['int']]

errors: List[str] = []
maxLearningTime: int = 0
session = requests.Session()

# ---------以下修改---------------------
def to_hex_byte_array(byte_array):
    return ''.join([f'{byte:02x}' for byte in byte_array])


def generate_cipher_text(password):
    # 获取当前时间戳（毫秒级）
    T0 = int(round(time.time() * 1000)) # 不清楚原因，网站和本地间有延迟，需手动补齐延迟
    # 模拟TextEncoder.encode
    P = password.encode('utf-8')
    V = (T0 >> 16) & 0xFF
    for byte in P:
        V ^= byte
    remainder = V % 100
    T1 = int((T0 / 100) * 100 + remainder)
    P1 = to_hex_byte_array(P)
    S = f"{T1}*" + P1
    S_encoded = S.encode('utf-8')
    # 模拟btoa
    E = base64.b64encode(S_encoded).decode('utf-8')
    return [E, T1]


# 登录
def login(user, pwd):
    while True:
        try:
            response = requests.get("https://welearn.sflep.com/user/prelogin.aspx?loginret=http://welearn.sflep.com/user/loginredirect.aspx")
            code_challenge = response.url.split("%26")[4].split("%3D")[1]
            state = response.url.split("%26")[6].split("%3D")[1]
            rturl = f"/connect/authorize/callback?client_id=welearn_web&redirect_uri=https%3A%2F%2Fwelearn.sflep.com%2Fsignin-sflep&response_type=code&scope=openid%20profile%20email%20phone%20address&code_challenge={code_challenge}&code_challenge_method=S256&state={state}&x-client-SKU=ID_NET472&x-client-ver=6.32.1.0"
            # 获取回调url
            print("登录中...", end='')
            while True:
                enpwd = generate_cipher_text(pwd)

                form_data = {
                    "rturl": rturl,
                    "account": user,
                    "pwd": str(enpwd[0]),
                    "ts": str(enpwd[1])
                }

                response = session.post("https://sso.sflep.com/idsvr/account/login", data=form_data)
                # print(response.json())
                
                code = response.json().get("code", -1)

                if code == 1:
                    print("\n帐号或密码错误！")
                    exit(0)

                session.get("https://welearn.sflep.com/user/prelogin.aspx?loginret=http://welearn.sflep.com/user/loginredirect.aspx")
                # response = session.get("https://welearn.sflep.com/student/index.aspx")

                if code == 0:
                    print("\n登录成功！")
                    return session
                
                print(".", end='')
        except:
            print("错误返回,登录失败！")
            print(f"返回信息：{response.json().get('msg', '未知错误')}")
            exit(0)
# ---------以上修改---------------------


def get_target_course_info():
    global cid, uid, classid, courseInfo

    # get course list
    response = session.get(
        'https://welearn.sflep.com/ajax/authCourse.aspx?action=gmc',
        headers={
            'Referer': 'https://welearn.sflep.com/2019/student/index.aspx'
        }
    )
    courseList = response.json()['clist']
    for index, course in enumerate(courseList, start=1):
        print('[id:{:>2d}]  完成度 {:>3d}%  {}'.format(index, course['per'], course['name']))

    #  get cid(course id) and uid(user id) and class id
    index = int(input('\n请输入需要刷时长的课程id（id为上方[]内的序号）: '))
    cid = str(courseList[index - 1]['cid'])
    response = session.get(
        'https://welearn.sflep.com/2019/student/course_info.aspx?cid=' + cid,
        headers={
            'Referer': 'https://welearn.sflep.com/2019/student/index.aspx'
        }
    )

    # ---------以下修改---------------------
    url = f"https://welearn.sflep.com/student/course_info.aspx?cid={cid}"
    response = session.get(url)
    # script = BeautifulSoup(response.text, "html.parser").find_all("script")[13]
    # print(script)
    # uid = re.search(r"uid=(\d+)", script.text).group(1)
    # classid = re.search(r"classid=(\d+)", script.text).group(1)

    uid = re.search('"uid":(.*?),', response.text).group(1)
    classid = re.search('"classid":"(.*?)"', response.text).group(1)
    # ---------以上修改---------------------

    # get target course's units
    req = session.get(
        'https://welearn.sflep.com/ajax/StudyStat.aspx',
        params={
            'action': 'courseunits',
            'cid': cid,
            'uid': uid
        },
        headers={
            'Referer': 'https://welearn.sflep.com/2019/student/course_info.aspx'
        }
    )
    courseInfo = req.json()['info']


def choose_unit():
    global unitIndex

    print("\n\n")
    print('[id: 0]  按顺序刷全部单元学习时长')
    for index, unit in enumerate(courseInfo, start=1):
        print(f"""[id:{index:>2d}]  {unit['unitname']}  {unit['name']}""")

    print("\n\n")
    unitIndex = int(input('请选择要刷时长的单元id（id为上方[]内的序号，输入0为刷全部单元）： '))


def input_time():
    global targetTime

    print("\n\n")
    print(dedent('''\
        模式1:每个练习增加指定学习时长，请直接输入时间
        如:希望每个练习增加30秒，则输入 30

        模式2:每个练习增加随机时长，请输入时间上下限并用英文逗号隔开
        如:希望每个练习增加10～30秒，则输入 10,30
    '''))
    print("\n\n")

    input_ = input("请严格按照以上格式输入: ")
    if(',' in input_):
        try:
            targetTime = [int(temp) for temp in input_.split(',')]
        except:
            print("格式异常")
            exit(0)
    else:
        targetTime = int(input_)


def generate_learning_time():
    """每一次模拟都重新生成一次随机学习时间"""
    global targetTime

    if type(targetTime) is int:
        learntime = targetTime
    else:
        learntime = random.randint(targetTime[0], targetTime[1])
    return learntime


def output_results():
    print('运行结束!!\n错误:', len(errors), '个')
    for index, error in enumerate(errors, start=1):
        print(f"第{index}个错误:{ error}")

    print("\n\n")
    print(dedent('''\
        **********  Created By Avenshy & SSmJaE  **********
                        Version : 0.3.0
           基于GPL3.0，完全开源，免费，禁止二次倒卖或商用
           https://www.github.com/Avenshy/WELearnToSleeep
                        仅供学习，勿滥用
        ***************************************************
    '''))
    print("\n\n")
    input("Press any key to exit...")


async def simulate(learningTime: int, chapter: Dict):
    print(f"""章节 : {chapter['location']}""")
    print(f"""已学 : {chapter['learntime']} 将学 : {learningTime}""")

    commonHeaders = {
        'Referer': 'https://welearn.sflep.com/student/StudyCourse.aspx'
    }

    scoid = chapter['id']
    commonData = {
        'uid': uid,
        'cid': cid,
        'scoid': scoid
    }

    await asyncio.sleep(REQUEST_INTERVAL)
    response = session.post(
        AJAX_URL,
        data={
            **commonData,
            'action': 'getscoinfo_v7',
        },
        headers=commonHeaders
    )

    if('学习数据不正确' in response.text):  # 重试
        await asyncio.sleep(REQUEST_INTERVAL)

        response = session.post(
            AJAX_URL,
            data={
                **commonData,
                'action': 'startsco160928',
            },
            headers=commonHeaders
        )
        response = session.post(
            AJAX_URL,
            data={
                **commonData,
                'action': 'getscoinfo_v7',
            },
            headers=commonHeaders
        )

        if('学习数据不正确' in response.text):
            print('\n错误:', chapter['location'])
            errors.append(chapter['location'])
            return

    returnJson = response.json()['comment']
    if('cmi' in returnJson):
        cmi = json.loads(returnJson)['cmi']

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

    await asyncio.sleep(REQUEST_INTERVAL)
    session.post(
        AJAX_URL,
        data={
            **commonData,
            'action': 'keepsco_with_getticket_with_updatecmitime',
            'session_time': session_time,
            'total_time': total_time
        },
        headers=commonHeaders
    )

    for currentTime in range(1, learningTime + 1):
        await asyncio.sleep(1)

        if(currentTime % 60 == 0):
            session.post(
                AJAX_URL,
                data={
                    **commonData,
                    'action': 'keepsco_with_getticket_with_updatecmitime',
                    'session_time': session_time,
                    'total_time': total_time},
                headers=commonHeaders
            )

    await asyncio.sleep(REQUEST_INTERVAL)
    session.post(
        AJAX_URL,
        data={
            **commonData,
            'action': 'savescoinfo160928',
            'crate': crate,
            'cstatus': cstatus,
            'status': 'unknown',
            'progress': progress,
            'trycount': '0'
        },
        headers=commonHeaders
    )


async def heartbeat():
    startTime = time.time()
    for _ in range(maxLearningTime+4*REQUEST_INTERVAL):
        print(
            f"""\r预计学习时长 : {maxLearningTime+4*REQUEST_INTERVAL} 已学习时长 : {int(time.time()-startTime)}""", end="")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def watcher():
    global maxLearningTime

    while True:
        get_target_course_info()
        choose_unit()
        input_time()

        if(unitIndex == 0):
            startIndex = 0
            endIndex = len(courseInfo)
        else:
            startIndex = unitIndex-1
            endIndex = unitIndex

        tasks = []
        for unit in range(startIndex, endIndex):
            response = session.get(
                f"https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={cid}&uid={uid}&unitidx={str(unit)}&classid={classid}",
                headers={
                    'Referer': 'https://welearn.sflep.com/2019/student/course_info.aspx?cid=' + cid
                }
            )

            for chapter in response.json()['info']:
                learningTime = generate_learning_time()

                if learningTime > maxLearningTime:
                    maxLearningTime = learningTime

                tasks.append(asyncio.create_task(simulate(learningTime, chapter)))

        await heartbeat()
        [await task for task in tasks]

        if (unitIndex == 0):  # 如果已经刷完所有单元
            break
        else:  # 如果只刷了指定单元
            print("\n\n")
            print(f'本单元结束！错误 : {len(errors)}个')

            for index, error in enumerate(errors, start=1):
                print(f"第{index}个错误章节 : {error}")

            print('回到选课处！！')
            print("\n\n")
            maxLearningTime = 0


async def main():
    await asyncio.gather(
        watcher()
    )

def welearn_time_run():
    user = input("请输入用户名=>")
    pwd = input("请输入密码=>")

    login(user, pwd)
    asyncio.run(main())
    output_results()
