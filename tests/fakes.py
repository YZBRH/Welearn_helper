"""测试用伪 tls_client.Session:按 URL 前缀返回预设响应,并记录所有请求。"""


class FakeResponse:
    def __init__(self, json_data=None, text="", headers=None, status_code=200):
        self._json = json_data
        self.text = text
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json data configured")
        return self._json


class FakeSession:
    def __init__(self):
        self.requests = []   # [(method, url, data), ...]
        self.routes = {}     # {url_prefix: FakeResponse 或 callable(url, data) -> FakeResponse}

    def get(self, url, **kwargs):
        return self._handle("GET", url, None)

    def post(self, url, data=None, **kwargs):
        return self._handle("POST", url, data)

    def _handle(self, method, url, data):
        self.requests.append((method, url, data))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp(url, data) if callable(resp) else resp
        raise AssertionError(f"未预置响应: {method} {url}")
