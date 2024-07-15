import requests
import base64
import time


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
            response = requests.get(
                "https://welearn.sflep.com/user/prelogin.aspx?loginret=http://welearn.sflep.com/user/loginredirect.aspx")
            code_challenge = response.url.split("%26")[4].split("%3D")[1]
            state = response.url.split("%26")[6].split("%3D")[1]
            rturl = f"/connect/authorize/callback?client_id=welearn_web&redirect_uri=https%3A%2F%2Fwelearn.sflep.com%2Fsignin-sflep&response_type=code&scope=openid%20profile%20email%20phone%20address&code_challenge={code_challenge}&code_challenge_method=S256&state={state}&x-client-SKU=ID_NET472&x-client-ver=6.32.1.0"
            # 获取回调url
            print("登录中...", end='')
            while True:
                msg = generate_cipher_text(pwd)

                form_data = {
                    "rturl": rturl,
                    "account": user,
                    "pwd": str(msg[0]),
                    "ts": str(msg[1])
                }
                if("帐号或密码错误" in session.post("https://sso.sflep.com/idsvr/account/login", data=form_data).text):
                    print("\n帐号或密码错误！")
                    exit(0)
                session.get(
                    "https://welearn.sflep.com/user/prelogin.aspx?loginret=http://welearn.sflep.com/user/loginredirect.aspx")
                # 登录

                response = session.get("https://welearn.sflep.com/student/index.aspx")
                if "WE Learn 随行课堂" in response.text:
                    print(f"\n登录成功！")
                    return session
                print(".", end='')
        except:
            print("错误返回,登录失败！")
            exit(0)


if __name__ == "__main__":
    session = requests.Session()
    login("15627617062", "mm1357924680")
