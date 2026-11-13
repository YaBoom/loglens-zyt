"""v0.1.11: tail 流式读取 + 记录边界回扩测试集（零依赖，标准库 unittest）。

两条契约：

契约一（性能）：`--tail` 不得把整个文件读进内存。旧实现 `f.readlines()`
会把 5GB 日志全部载入再取最后 N 行——klogg/glogg/lnav 的共同卖点恰恰是
"磁盘直读、只保留窗口"。本测试用临时文件验证正确性，性能契约通过
"窗口行数与请求一致"间接锁定（全量读不会错，只是慢，无法在单测中断言内存）。

契约二（边界）：窗口起点若切在多行记录中间（堆栈帧 / Caused by /
Python traceback 首行），必须向前回扩到完整记录边界，否则触发错误的
ERROR 行整个丢失，孤儿帧各自变成 UNKNOWN 记录——报告显示"N 行碎片、
0 个错误"，与真实情况"1 个错误、堆栈缺头"完全背离。

回扩同样保守：只补明确像被切断的行，绝不为了凑整多吞记录。

运行: python -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest

from loglens.analyzer import LogAnalyzer


def _make_log(lines):
    """把行列表写成临时文件，返回路径（测试结束自动清理）。"""
    fd, path = tempfile.mkstemp(suffix=".log", text=False)
    with os.fdopen(fd, "wb") as f:
        f.write("\n".join(lines).encode("utf-8"))
    return path


class TailWindowTest(unittest.TestCase):
    """_tail_window 窗口裁剪与边界回扩"""

    def setUp(self):
        self.analyzer = LogAnalyzer(api_key="")
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            if os.path.exists(p):
                os.remove(p)

    def _tmp(self, lines):
        path = _make_log(lines)
        self._paths.append(path)
        return path

    def test_tail_returns_requested_line_count(self):
        """tail=5 恰好返回最后 5 行（性能契约的行数锚点）"""
        lines = [f"2026-11-13 09:{i:02d}:00 INFO [app] msg {i}" for i in range(100)]
        path = self._tmp(lines)
        window = self.analyzer._tail_window(path, 5)
        self.assertEqual(len(window), 5)
        self.assertIn("msg 99", window[-1])
        self.assertIn("msg 95", window[0])

    def test_tail_larger_than_file_returns_all(self):
        """tail 超过文件总行数时返回全部行，等价于不截断"""
        lines = [f"2026-11-13 09:{i:02d}:00 INFO [app] msg {i}" for i in range(10)]
        path = self._tmp(lines)
        window = self.analyzer._tail_window(path, 1000)
        self.assertEqual(len(window), 10)

    def test_window_starting_mid_stack_expands_to_header(self):
        """核心契约：窗口起点切在堆栈中间 → 回扩到触发它的 ERROR 行。

        构造：1 条 ERROR + 40 帧堆栈 + 5 条 INFO 垫底。
        tail=15 时旧实现窗口起点在第 25 帧附近，ERROR 行丢失。
        """
        header = '2026-11-13 09:00:00.000 ERROR 1 --- [main] c.a.OrderService : NullPointerException: Cannot invoke "Order.getId()"'
        frames = [f"\tat com.acme.OrderService.step{i}(OrderService.java:{i})" for i in range(40)]
        filler = [f"2026-11-13 09:01:{i:02d}.000 INFO 1 --- [main] c.a.Heartbeat : ok {i}" for i in range(5)]
        path = self._tmp([header] + frames + filler)

        report = self.analyzer.analyze(path, tail=15)
        # 旧实现：15 条 UNKNOWN 碎片、0 个错误；新实现：1 个错误
        self.assertEqual(report.error_count, 1)
        # 样本顶帧必须是真实的堆栈帧，而不是某个孤儿帧被当成独立记录
        self.assertTrue(report.error_samples)
        self.assertIn("at com.acme.OrderService.step", report.error_samples[0])

    def test_window_starting_at_caused_by_expands(self):
        """窗口起点是 Caused by: 分节 → 回扩到外层 ERROR 行（根因帧才能与异常关联）"""
        lines = [
            "2026-11-13 09:00:00.000 ERROR 1 --- [main] c.a.PaymentService : PaymentFailedException: charge failed",
            "\tat com.acme.pay.PaymentService.charge(PaymentService.java:120)",
            "Caused by: java.sql.SQLException: No suitable driver found",
            "\tat com.acme.pay.Driver.load(Driver.java:33)",
            "2026-11-13 09:00:01.000 INFO 1 --- [main] c.a.Health : ok",
        ]
        path = self._tmp(lines)
        # tail=3 旧实现窗口 = [Caused by, Driver.load, INFO]，外层 ERROR 丢失
        report = self.analyzer.analyze(path, tail=3)
        self.assertEqual(report.error_count, 1)
        self.assertIn("PaymentFailedException", report.error_samples[0])

    def test_window_starting_at_traceback_expands(self):
        """窗口起点是 Python 的 "Traceback (most recent call last):" → 回扩到 ERROR 行"""
        lines = [
            "2026-11-13 09:00:00 ERROR [worker] job crashed",
            "Traceback (most recent call last):",
            '  File "pipeline.py", line 12, in run',
            "    payload = json.loads(raw)",
            "ValueError: bad payload",
            "2026-11-13 09:00:01 INFO [worker] done",
        ]
        path = self._tmp(lines)
        # tail=4 旧实现窗口 = [Traceback, File..., payload..., ValueError]
        report = self.analyzer.analyze(path, tail=4)
        self.assertEqual(report.error_count, 1)
        # 样本取记录的 raw 首行 + 顶帧，回扩成功意味着 ERROR 行回来了
        self.assertIn("job crashed", report.error_samples[0])
        self.assertIn("↳", report.error_samples[0])

    def test_window_starting_at_record_start_no_expansion(self):
        """窗口起点本来就是完整记录起始行 → 不回扩，行为与旧版一致"""
        lines = [f"2026-11-13 09:{i:02d}:00 INFO [app] msg {i}" for i in range(20)]
        path = self._tmp(lines)
        window = self.analyzer._tail_window(path, 10)
        self.assertEqual(len(window), 10)
        self.assertIn("msg 19", window[-1])

    def test_expansion_does_not_cross_multiple_independent_records(self):
        """回扩遇独立记录即停：tail=1 取最后一行（独立 INFO 记录）时，
        不应向前吞掉前面的孤儿文本行——它既不是续行也不是 traceback"""
        lines = [
            "some orphan text line without timestamp",
            "2026-11-13 09:00:00 INFO [app] real record",
        ]
        path = self._tmp(lines)
        window = self.analyzer._tail_window(path, 1)
        self.assertEqual(len(window), 1)
        self.assertIn("real record", window[0])
        self.assertNotIn("orphan", window[0])

    def test_tail_result_consistent_with_full_analysis(self):
        """窗口回扩后，tail 模式与全量分析对同一段日志给出相同错误数"""
        header = "2026-11-13 09:00:00.000 ERROR 1 --- [main] c.a.Billing : TimeoutException: downstream slow"
        frames = [f"\tat com.acme.Billing.call{ i}(Billing.java:{i})" for i in range(30)]
        filler = [f"2026-11-13 09:01:{i:02d}.000 INFO 1 --- [main] c.a.H : ok {i}" for i in range(10)]
        path = self._tmp([header] + frames + filler)

        full = self.analyzer.analyze(path)
        # tail=35：窗口 = 最后 5 帧 + 10 条 INFO，起点切在堆栈中间 → 回扩补全
        # （tail=10 时窗口全是 INFO 垫底行，0 个错误本来就是正确结果）
        tail_report = self.analyzer.analyze(path, tail=35)
        self.assertEqual(full.error_count, tail_report.error_count)
        self.assertEqual(full.error_samples, tail_report.error_samples)


class TailStreamReadTest(unittest.TestCase):
    """_read_tail_lines 流式读取的正确性（块边界 / 无尾换行 / 单块覆盖）"""

    def setUp(self):
        self.analyzer = LogAnalyzer(api_key="")
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            if os.path.exists(p):
                os.remove(p)

    def _tmp(self, lines):
        path = _make_log(lines)
        self._paths.append(path)
        return path

    def test_matches_full_readlines(self):
        """流式尾部读取结果必须与 f.readlines() 逐行一致"""
        import random
        random.seed(42)
        # 行长不一，制造跨块边界（块大小 64KB，行总长约 300KB）
        lines = [
            f"2026-11-13 09:00:{i % 60:02d} INFO [app] {'x' * random.randint(10, 400)} {i}"
            for i in range(1000)
        ]
        path = self._tmp(lines)
        got = self.analyzer._read_tail_lines(path, 100000)
        self.assertEqual(got, lines)

    def test_file_without_trailing_newline(self):
        """最后一行没有换行符时也必须完整返回"""
        path = self._tmp(["a", "b", "last-no-newline"])
        got = self.analyzer._read_tail_lines(path, 10)
        self.assertEqual(got, ["a", "b", "last-no-newline"])

    def test_small_file_single_block(self):
        """小文件一个块就覆盖，结果与内容一致"""
        lines = ["l1", "l2", "l3"]
        path = self._tmp(lines)
        got = self.analyzer._read_tail_lines(path, 2)
        self.assertEqual(got, ["l2", "l3"])


if __name__ == "__main__":
    unittest.main()
