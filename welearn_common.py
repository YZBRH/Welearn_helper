"""Welearn_helper 共享核心:异常、登录加密、参数解析、登录与课程查询(CLI 与 GUI 共用)。"""
import base64
import re
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
