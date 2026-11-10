"""v0.1.8: loglens web 本地 Web 界面的契约测试。

用 ThreadingHTTPServer 绑定端口 0（OS 分配空闲端口）+ urllib 发请求，
不依赖任何测试框架之外的库。运行前清空 DEEPSEEK_API_KEY，
保证分析走本地路径、测试结果稳定可复现。
"""

import json
import os
import threading
import unittest
import urllib.request

from loglens.web import create_server, PAGE_HTML

# LLM 未配置时分析自动回退本地；这里显式清掉，防止测试机带了 key 走外部调用
os.environ.pop("DEEPSEEK_API_KEY", None)

JAVA_LOG = """2026-03-16 10:23:45 INFO  UserService - login attempt user=u1
2026-03-16 10:23:46 ERROR java.lang.NullPointerException: user is null
    at com.demo.UserService.getUser(UserService.java:42)
    at com.demo.AuthController.login(AuthController.java:18)
2026-03-16 10:23:47 INFO  UserService - retry"""


class _ServerFixture:
    """每个测试类共用一个跑在后台线程的临时服务实例。"""

    server = None
    base_url = None

    @classmethod
    def start(cls):
        cls.server = create_server("127.0.0.1", 0)
        port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{port}"
        t = threading.Thread(target=cls.server.serve_forever, daemon=True)
        t.start()

    @classmethod
    def stop(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ServerFixture.start()
        cls.base_url = _ServerFixture.base_url

    @classmethod
    def tearDownClass(cls):
        _ServerFixture.stop()

    def _get(self, path):
        with urllib.request.urlopen(self.base_url + path, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    # ---------- 页面 ----------

    def test_index_served_with_upload_ui(self):
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        page = body.decode("utf-8")
        # README 承诺的"拖拽上传"必须真实出现在页面上
        self.assertIn("拖", page)
        self.assertIn("textarea", page)
        # 隐私声明: 同类工具最大卖点，页面必须明示
        self.assertIn("127.0.0.1", page)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    # ---------- 分析接口 ----------

    def test_analyze_reports_error(self):
        status, data = self._post("/analyze", {"content": JAVA_LOG, "level": "ERROR"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertGreater(data["stats"]["error_count"], 0)
        # v0.1.7 的归并能力必须穿透到 web: 堆栈样本要带顶帧
        samples = "\n".join(data["error_samples"])
        self.assertIn("NullPointerException", samples)
        self.assertIn("UserService.java:42", samples)
        self.assertTrue(data["suggestions"])
        self.assertTrue(data["root_cause"])

    def test_analyze_respects_level_filter(self):
        # INFO 行不算错误；有 ERROR 行时错误计数为 1（多行堆栈已归并成一个事件）
        _, data_ok = self._post("/analyze", {"content": JAVA_LOG, "level": "ERROR"})
        _, data_info = self._post("/analyze", {"content": JAVA_LOG, "level": "INFO"})
        self.assertGreaterEqual(data_ok["stats"]["error_count"], 1)
        self.assertGreaterEqual(data_info["stats"]["error_count"], data_ok["stats"]["error_count"])

    def test_analyze_empty_content_is_ok(self):
        status, data = self._post("/analyze", {"content": "  \n ", "level": "ERROR"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["stats"]["error_count"], 0)

    def test_analyze_invalid_json_rejected(self):
        req = urllib.request.Request(
            self.base_url + "/analyze", data=b"not-json",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_analyze_invalid_level_falls_back_to_error(self):
        # 非法级别不应 500，静默归一到 ERROR
        status, data = self._post("/analyze", {"content": JAVA_LOG, "level": "bogus"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])


class TestWebCliContract(unittest.TestCase):
    """CLI 契约: web 子命令必须真实存在（README 承诺过它）。"""

    def test_web_command_registered(self):
        from click.testing import CliRunner
        from loglens.cli import main
        result = CliRunner().invoke(main, ["web", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("web", result.output.lower())

    def test_page_html_is_valid_structure(self):
        # 冒烟级校验: 页面模板至少包含渲染结果所需的 JS 函数与容器
        for token in ("run()", "render(", "/analyze", 'id="out"', "dragover"):
            self.assertIn(token, PAGE_HTML)


if __name__ == "__main__":
    unittest.main()
