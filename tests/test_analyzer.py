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


class TestLocalReportCoherence(unittest.TestCase):
    """v0.1.6: 摘要/根因/建议必须同一个口径。

    分类改准了但结论仍按 if/elif 命中顺序走，就会出现
    「摘要说 ConnectionTimeout 最多、根因却说空指针」的自相矛盾。
    """

    NOISY_LOG = (
        "2026-10-22 09:00:01 ERROR [db] connection timeout after 3000ms\n"
        "2026-10-22 09:00:02 ERROR [db] connection timeout after 5000ms\n"
        "2026-10-22 09:00:03 ERROR [db] connection timeout after 8000ms\n"
        "2026-10-22 09:00:04 ERROR [order] NullPointerException: order is null\n"
    )

    def setUp(self):
        self.analyzer = LogAnalyzer()

    def test_summary_sorted_by_count_desc(self):
        report = self.analyzer.analyze_stdin(self.NOISY_LOG)
        rows = report.summary.splitlines()[1:]
        counts = [int(row.rsplit(":", 1)[1].strip().rstrip("个")) for row in rows]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertIn("ConnectionTimeout: 3 个", rows[0])

    def test_root_cause_follows_most_frequent_type(self):
        report = self.analyzer.analyze_stdin(self.NOISY_LOG)
        self.assertIn("最高频", report.root_cause)  # 结论必须声明依据
        self.assertIn("ConnectionTimeout", report.root_cause)
        self.assertNotIn("空指针", report.root_cause)

    def test_suggestions_numbered_once(self):
        report = self.analyzer.analyze_stdin(self.NOISY_LOG)
        prefixes = [s.split(".")[0] for s in report.suggestions]
        self.assertEqual(prefixes, [str(i) for i in range(1, len(report.suggestions) + 1)])

    def test_generic_fallback_when_no_hint(self):
        report = self.analyzer.analyze_stdin(
            "2026-10-22 09:00:01 ERROR [x] something odd happened"
        )
        self.assertIn("异常", report.root_cause)
        self.assertTrue(report.suggestions)


if __name__ == "__main__":
    unittest.main()
