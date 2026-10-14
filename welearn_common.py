"""Welearn_helper 共享核心:异常、登录加密、参数解析、登录与课程查询(CLI 与 GUI 共用)。"""
import base64
import re
import threading
import time
from typing import Callable, List, Optional, Tuple, Union

import tls_client

AJAX_BASE = "https://welearn.sflep.com"


class LoginError(Exception):
    """账号密码错误或登录流程失败"""


class WelearnError(Exception):
    """业务流程异常(响应异常、空课程列表、被用户停止等)"""


def new_session() -> tls_client.Session:
    return tls_client.Session(client_identifier="chrome_120", debug=False)


def to_hex_byte_array(byte_array) -> str:
    return ''.join([f'{byte:02x}' for byte in byte_array])


def generate_cipher_text(password: str, now_ms: Optional[int] = None) -> Tuple[str, int]:
    # 获取当前时间戳(毫秒级);不清楚原因,网站和本地间有延迟,需手动补齐延迟
    T0 = int(round(now_ms if now_ms is not None else time.time() * 1000))
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
    return E, T1


def parse_numeric_param(text: str) -> Union[int, Tuple[int, int]]:
    """解析 '30' -> 30;'10,30' -> (10, 30);其余抛 ValueError(替代原 eval 用法)。"""
    text = text.strip()
    if ',' in text:
        parts = text.split(',')
        if len(parts) != 2:
            raise ValueError(f"格式异常: {text!r}(区间格式为 最小值,最大值)")
        lo, hi = int(parts[0]), int(parts[1])
        if lo > hi:
            raise ValueError(f"格式异常: {text!r}(下限大于上限)")
        return lo, hi
    return int(text)


def _noop(msg):
    pass


def login(session, user: str, pwd: str,
          on_log: Callable[[str], None] = _noop,
          stop_event: Optional[threading.Event] = None) -> None:
    """在传入的 session 上完成登录;密码错误抛 LoginError,被停止抛 WelearnError。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            raise WelearnError("已停止")
        try:
            response = session.get(
                "https://welearn.sflep.com/user/prelogin.aspx?loginret=http://welearn.sflep.com/user/loginredirect.aspx")
            rurl = response.headers.get('Location')

            code_challenge = rurl.split("&")[4].split("=")[1]
            state = rurl.split("&")[6].split("=")[1]

            rturl = (f"/connect/authorize/callback?client_id=welearn_web"
                     f"&redirect_uri=https%3A%2F%2Fwelearn.sflep.com%2Fsignin-sflep"
                     f"&response_type=code&scope=openid%20profile%20email%20phone%20address"
                     f"&code_challenge={code_challenge}&code_challenge_method=S256"
                     f"&state={state}&x-client-SKU=ID_NET472&x-client-ver=6.32.1.0")
            on_log("登录中...")
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise WelearnError("已停止")
                enpwd = generate_cipher_text(pwd)

                form_data = {
                    "rturl": rturl,
                    "account": user,
                    "pwd": str(enpwd[0]),
                    "ts": str(enpwd[1])
                }

                response = session.post("https://sso.sflep.com/idsvr/account/login", data=form_data)

                rt_json = response.json()
                code = rt_json.get("code", -1)

                if code == -1:
                    continue

                if code == 1:
                    raise LoginError("帐号或密码错误!")

                next_url = "https://sso.sflep.com/idsvr" + rt_json.get("data")

                while True:
                    resp = session.get(next_url)
                    if resp.status_code in (301, 302, 303, 307, 308):
                        next_url = resp.headers.get("Location", None)
                        if not next_url:
                            break
                    else:
                        break

                if code == 0:
                    on_log("登录成功!")
                    return
        except (LoginError, WelearnError):
            raise
        except Exception as e:
            raise LoginError(f"错误返回,登录失败!错误信息:{e}")


def fetch_courses(session) -> List[dict]:
    response = session.get(
        'https://welearn.sflep.com/ajax/authCourse.aspx?action=gmc',
        headers={'Referer': 'https://welearn.sflep.com/2019/student/index.aspx'})
    clist = response.json().get('clist', [])
    if not clist:
        raise WelearnError('发生错误!!!可能是登录错误或没有课程!!!')
    return clist


def get_course_ids(session, cid: str) -> Tuple[str, str]:
    response = session.get(f"https://welearn.sflep.com/student/course_info.aspx?cid={cid}")
    uid = re.search('"uid":(.*?),', response.text).group(1)
    classid = re.search('"classid":"(.*?)"', response.text).group(1)
    return uid, classid


def fetch_units(session, cid: str, uid: str) -> List[dict]:
    response = session.get(
        'https://welearn.sflep.com/ajax/StudyStat.aspx',
        params={'action': 'courseunits', 'cid': cid, 'uid': uid},
        headers={'Referer': 'https://welearn.sflep.com/2019/student/course_info.aspx'})
    return response.json()['info']


def fetch_sections(session, cid: str, uid: str, unitidx: int, classid: str) -> List[dict]:
    """只读拉取指定单元的小节列表(scoLeaves,与刷课遍历同一接口;不提交任何数据)"""
    response = session.get(
        f'https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={cid}&uid={uid}&unitidx={unitidx}&classid={classid}',
        headers={'Referer': f'https://welearn.sflep.com/2019/student/course_info.aspx?cid={cid}'})
    if "异常" in response.text or "出错了" in response.text:
        raise WelearnError('单元不存在或已到末尾!!!')
    return response.json()["info"]
