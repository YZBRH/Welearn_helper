"""控制台输出治理:Werkzeug 的彩色提示改写到 _logs/access.log,控制台只留本项目自己的输出。"""
import logging

import pytest

import web_gui


@pytest.fixture()
def werkzeug_logger():
    """werkzeug 是全局 logger,用例结束后还原,避免影响其它测试"""
    logger = logging.getLogger("werkzeug")
    saved = (list(logger.handlers), logger.propagate, logger.level, list(logger.filters))
    yield logger
    logger.handlers = saved[0]
    logger.propagate = saved[1]
    logger.setLevel(saved[2])
    logger.filters = saved[3]


def flush_all(logger):
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()


class TestAccessLogRedirect:
    def test_logger_stops_propagating_to_console(self, tmp_path, monkeypatch, werkzeug_logger):
        monkeypatch.setattr(web_gui, "LOG_DIR", str(tmp_path))
        handler = web_gui._setup_werkzeug_logging()

        assert werkzeug_logger.propagate is False, "仍会落回 root → lastResort 打到 stderr"
        assert handler in werkzeug_logger.handlers
        assert werkzeug_logger.level == logging.INFO

    def test_ansi_colors_stripped_in_file(self, tmp_path, monkeypatch, werkzeug_logger):
        monkeypatch.setattr(web_gui, "LOG_DIR", str(tmp_path))
        web_gui._setup_werkzeug_logging()

        werkzeug_logger.info("\x1b[31m\x1b[1mWARNING: This is a development server\x1b[0m")
        werkzeug_logger.info('127.0.0.1 - - "GET /favicon.ico HTTP/1.1" 404 -')
        flush_all(werkzeug_logger)

        text = (tmp_path / "access.log").read_text(encoding="utf-8")
        assert "WARNING: This is a development server" in text
        assert '"GET /favicon.ico HTTP/1.1" 404' in text
        assert "\x1b[" not in text, "日志文件里不应残留颜色码"

    def test_status_poll_not_written(self, tmp_path, monkeypatch, werkzeug_logger):
        monkeypatch.setattr(web_gui, "LOG_DIR", str(tmp_path))
        web_gui._setup_werkzeug_logging()

        werkzeug_logger.info('127.0.0.1 - - "GET /api/status?after=12 HTTP/1.1" 200 -')
        werkzeug_logger.info('127.0.0.1 - - "POST /api/login HTTP/1.1" 200 -')
        flush_all(werkzeug_logger)

        text = (tmp_path / "access.log").read_text(encoding="utf-8")
        assert "/api/status" not in text
        assert "/api/login" in text

    def test_setup_is_idempotent(self, tmp_path, monkeypatch, werkzeug_logger):
        monkeypatch.setattr(web_gui, "LOG_DIR", str(tmp_path))
        web_gui._setup_werkzeug_logging()
        web_gui._setup_werkzeug_logging()
        assert len(werkzeug_logger.handlers) == 1, "重复初始化不应叠加 handler"

    def test_unwritable_dir_does_not_crash(self, monkeypatch, werkzeug_logger):
        monkeypatch.setattr(web_gui, "LOG_DIR", "\0bad\0path")
        handler = web_gui._setup_werkzeug_logging()   # 不抛异常即可
        assert isinstance(handler, logging.NullHandler)


class TestBanner:
    def test_main_silences_flask_banner(self, monkeypatch, tmp_path, werkzeug_logger):
        monkeypatch.setattr(web_gui, "LOG_DIR", str(tmp_path))
        monkeypatch.setenv("WELEARN_NO_OPEN", "1")
        monkeypatch.setattr(web_gui.flask_cli, "show_server_banner",
                            lambda *args, **kwargs: "原始横幅")

        def fake_run(**kwargs):
            raise SystemExit(0)

        monkeypatch.setattr(web_gui.app, "run", fake_run)
        with pytest.raises(SystemExit):
            web_gui.main()

        assert web_gui.flask_cli.show_server_banner() is None

    def test_page_declares_inline_favicon(self):
        """<link rel="icon" href="data:,"> 让浏览器不再请求 /favicon.ico,404 从源头消失"""
        assert '<link rel="icon" href="data:,">' in web_gui.INDEX_HTML
