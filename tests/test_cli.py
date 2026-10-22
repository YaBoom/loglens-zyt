"""v0.1.4: CLI 契约测试（零依赖，使用标准库 unittest + click.testing.CliRunner）。

重点守住"帮助文本/ README 承诺给用户"的用法，尤其是 v0.1.3 修复的
stdin 管道 —— 该 bug 因缺少测试而长期腐化。
运行: python -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest

from click.testing import CliRunner

from loglens.cli import main
from loglens import __version__

# 测试期间禁用 LLM: 保证走本地分析，不发起网络请求、结果可重复
TEST_ENV = {"DEEPSEEK_API_KEY": ""}

SAMPLE_LOG = (
    "2026-10-19 10:00:00 ERROR [db] ConnectionTimeout: timeout after 30s\n"
    "2026-10-19 10:00:01 INFO [app] started\n"
)


class TestAnalyzeCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_stdin_pipe(self):
        """v0.1.3 回归: grep ERROR app.log | loglens analyze - 必须可用"""
        result = self.runner.invoke(
            main, ["analyze", "-"], input=SAMPLE_LOG, env=TEST_ENV
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("stdin 管道输入", result.output)
        self.assertIn("1", result.output)  # 分析行数非空

    def test_missing_file_error(self):
        result = self.runner.invoke(main, ["analyze", "no_such_file.log"], env=TEST_ENV)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("文件不存在: no_such_file.log", result.output)

    def test_regular_file(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".log", delete=False, encoding="utf-8"
        ) as f:
            f.write(SAMPLE_LOG)
            path = f.name
        try:
            result = self.runner.invoke(main, ["analyze", path], env=TEST_ENV)
            self.assertEqual(result.exit_code, 0, result.output)
            # 报告对外承诺的是"错误计数"，原始错误行不在报告内（见 axiom 候选清单）
            self.assertIn("检测到 1 个错误", result.output)
            self.assertIn("错误数量", result.output)
        finally:
            os.unlink(path)

    def test_output_report_written(self):
        """-o 必须真正落盘报告文件"""
        with tempfile.TemporaryDirectory() as d:
            out_path = os.path.join(d, "report.md")
            result = self.runner.invoke(
                main, ["analyze", "-", "-o", out_path], input=SAMPLE_LOG, env=TEST_ENV
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("LogLens 分析报告", body)
            self.assertIn("错误数量: 1", body)

    def test_version_flag(self):
        """v0.1.6: 版本号只有 loglens.__version__ 一个来源，避免多处手写漂移"""
        result = self.runner.invoke(main, ["--version"], env=TEST_ENV)
        self.assertEqual(result.exit_code, 0)
        self.assertIn(__version__, result.output)


if __name__ == "__main__":
    unittest.main()
