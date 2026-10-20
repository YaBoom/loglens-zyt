"""v0.1.5: 分析器测试集（零依赖，标准库 unittest）。

重点覆盖"报告包含原始错误行样本"这一报告可读性契约：
报告不能只有计数和根因推断，必须能核对具体报错内容。
运行: python -m unittest discover -s tests -v
"""

import unittest

from loglens.analyzer import LogAnalyzer


SAMPLE_LOG = """2026-10-20 09:00:01 ERROR [db] ConnectionTimeout: connection timeout after 5000ms
2026-10-20 09:00:02 ERROR [db] ConnectionTimeout: connection timeout after 5001ms
2026-10-20 09:00:03 ERROR [order] NullPointerException: order is null
2026-10-20 09:00:04 ERROR [order] NullPointerException: cart is null
2026-10-20 09:00:05 ERROR [order] SQLException: deadlocked on table orders
2026-10-20 09:00:06 INFO [app] service started
"""


class TestErrorSamples(unittest.TestCase):
    """报告错误样本收集"""

    def setUp(self):
        self.analyzer = LogAnalyzer()  # 无 API key，走本地分析

    def test_report_contains_samples(self):
        report = self.analyzer.analyze_stdin(SAMPLE_LOG)
        self.assertTrue(report.error_samples, "报告必须包含原始错误行样本")

    def test_samples_deduped_by_error_type(self):
        report = self.analyzer.analyze_stdin(SAMPLE_LOG)
        # 4 类错误各保留一条: ConnectionTimeout / NullPointerException / SQLException
        self.assertEqual(len(report.error_samples), 3)

    def test_samples_are_raw_lines(self):
        report = self.analyzer.analyze_stdin(SAMPLE_LOG)
        joined = "\n".join(report.error_samples)
        self.assertIn("ConnectionTimeout", joined)
        self.assertIn("2026-10-20", joined)  # 原始行应带时间戳

    def test_samples_truncated_for_long_lines(self):
        long_line = "2026-10-20 09:00:07 ERROR [x] ConnectionTimeout: " + "a" * 500
        report = self.analyzer.analyze_stdin(long_line)
        self.assertTrue(all(len(s) <= 301 for s in report.error_samples))  # 300 + 省略号

    def test_no_errors_no_samples(self):
        report = self.analyzer.analyze_stdin("2026-10-20 09:00:00 INFO [app] all good")
        self.assertEqual(report.error_samples, [])

    def test_default_limit(self):
        lines = "\n".join(
            f"2026-10-20 09:00:{i:02d} ERROR [x] X{i}Exception: boom" for i in range(10)
        )
        report = self.analyzer.analyze_stdin(lines)
        self.assertEqual(len(report.error_samples), 5)


if __name__ == "__main__":
    unittest.main()
