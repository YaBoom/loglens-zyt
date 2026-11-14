"""v0.1.7: 多行堆栈归并测试集（零依赖，标准库 unittest）。

核心契约（背调结论）：一条 Java 异常 + 30 行堆栈必须归并为 1 条记录，
否则堆栈帧会变成 30 条无上下文的 UNKNOWN 行，既污染样本也污染统计。

第二契约（同样重要）：**宁可拆开，不可粘错**。合并过宽会让两条独立日志
变成一条、错误计数静默变少，比刷屏更难发现。所以反例与正例同等重要。

运行: python -m unittest discover -s tests -v
"""

import unittest

from loglens.analyzer import LogAnalyzer
from loglens.parser import LogParser

# 真实 Spring Boot 输出：logger 与冒号间带空格，异常行后跟 4 帧
JAVA_STACK_LOG = """2026-11-09 09:15:00.123 ERROR 12345 --- [http-nio-8080-exec-1] c.a.OrderService : NullPointerException: Cannot invoke "Order.getId()" because "order" is null
	at com.acme.order.OrderService.process(OrderService.java:42)
	at com.acme.order.OrderController.submit(OrderController.java:88)
	at java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0(Native Method)
	at java.base/java.lang.Thread.run(Thread.java:833)
2026-11-09 09:15:01.456 INFO 12345 --- [http-nio-8080-exec-2] c.a.HealthController : health check ok
"""

CHAINED_CAUSE_LOG = """2026-11-09 09:20:00.001 ERROR 1 --- [main] c.a.PaymentService : PaymentFailedException: charge failed
	at com.acme.pay.PaymentService.charge(PaymentService.java:120)
Caused by: java.sql.SQLException: No suitable driver found for jdbc:mysql://db:3306/prod
	at com.acme.pay.Driver.load(Driver.java:33)
	... 23 more
"""

PY_TRACEBACK_LOG = """2026-11-09 09:30:00,100 - worker - ERROR - job crashed
Traceback (most recent call last):
  File "/app/worker.py", line 42, in run
    handle(job)
  File "/app/handler.py", line 17, in handle
    raise ValueError("bad payload")
ValueError: bad payload
2026-11-09 09:30:01,100 - worker - INFO - next job
"""


class TestStackMerging(unittest.TestCase):
    """正例：多行必须归并"""

    def setUp(self):
        self.parser = LogParser()

    def test_java_stack_is_one_entry(self):
        entries = self.parser.parse(JAVA_STACK_LOG)
        self.assertEqual(len(entries), 2, "5 行堆栈 + 1 行 INFO 应归并为 2 条记录")
        error = entries[0]
        self.assertEqual(error.level, "ERROR")
        self.assertIn("NullPointerException", error.message)
        self.assertEqual(len(error.stack), 4)
        self.assertIn("OrderService.java:42", error.stack[0])
        self.assertIn("\n", error.raw, "归并后 raw 应保留完整堆栈")
        # 堆栈帧不能被当成 INFO（旧实现按"匹配不到级别就 UNKNOWN"处理）
        self.assertEqual(entries[1].level, "INFO")
        self.assertEqual(entries[1].stack, [])

    def test_chained_cause_and_more_frames_merge(self):
        entries = self.parser.parse(CHAINED_CAUSE_LOG)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0].stack), 4)
        self.assertTrue(any("Caused by:" in s for s in entries[0].stack))
        self.assertTrue(any("23 more" in s for s in entries[0].stack))

    def test_blank_line_before_caused_by_does_not_split(self):
        """Java 有时在 Caused by 前留空行，空行不能截断因果链"""
        log = CHAINED_CAUSE_LOG.replace("Caused by:", "\nCaused by:")
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 1)

    def test_bare_print_stack_trace_becomes_error(self):
        """printStackTrace() 直出：无时间戳无级别，但有堆栈 + 可识别类型 → 算错误"""
        log = (
            "java.lang.NullPointerException: order is null\n"
            "\tat com.acme.OrderService.process(OrderService.java:42)\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].level, "ERROR")
        self.assertEqual(len(entries[0].stack), 1)

    def test_python_traceback_merges_into_error_record(self):
        """traceback 不是独立事件，而是 ERROR 行的后续 —— 一次故障只能算一条错误"""
        entries = self.parser.parse(PY_TRACEBACK_LOG)
        self.assertEqual(len(entries), 2, "ERROR + traceback 应合成 1 条，再加 1 条 INFO")
        tb = entries[0]
        self.assertIn("job crashed", tb.message)      # 业务上下文不丢
        self.assertIn("ValueError: bad payload", tb.message)  # 异常类型不丢
        self.assertEqual(tb.level, "ERROR")
        self.assertTrue(any("Traceback" in s for s in tb.stack))
        self.assertTrue(any('File "/app/handler.py"' in s for s in tb.stack))
        self.assertEqual(entries[1].level, "INFO")

    def test_traceback_at_start_of_log(self):
        """日志从堆栈开始（没有表头 ERROR 行）时，异常行即报错内容"""
        log = (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 9, in <module>\n'
            "    run()\n"
            "KeyError: 'token'\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].message, "KeyError: 'token'")
        self.assertEqual(entries[0].level, "ERROR")


class TestNoOverMerging(unittest.TestCase):
    """反例：不该合并的绝不合并"""

    def setUp(self):
        self.parser = LogParser()

    def test_indented_record_start_starts_new_entry(self):
        log = (
            "2026-11-09 09:00:00 ERROR [a] boom\n"
            "  2026-11-09 09:00:01 ERROR [b] second failure\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 2, "带缩进的时间戳行仍是新记录，不得粘上一条")
        self.assertEqual(entries[1].source, "b")

    def test_two_errors_with_stacks_do_not_bleed(self):
        log = (
            "2026-11-09 09:00:00 ERROR [a] NullPointerException: x is null\n"
            "\tat com.a.A(A.java:1)\n"
            "2026-11-09 09:00:01 ERROR [b] SQLException: deadlock\n"
            "\tat com.b.B(B.java:2)\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(entries[0].stack), 1)
        self.assertEqual(len(entries[1].stack), 1)
        self.assertIn("A.java:1", entries[0].stack[0])
        self.assertIn("B.java:2", entries[1].stack[0])

    def test_json_lines_are_not_merged(self):
        log = (
            '{"level":"error","msg":"db timeout"}\n'
            '{"level":"info","msg":"ok"}\n'
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].stack, [])


class TestStackInReport(unittest.TestCase):
    """归并结果必须穿透到报告，否则等于没做"""

    JAVA = (
        "2026-11-09 09:15:00 ERROR [order] NullPointerException: order is null\n"
        "\tat com.acme.order.OrderService.process(OrderService.java:42)\n"
        "\tat com.acme.order.OrderController.submit(OrderController.java:88)\n"
        "2026-11-09 09:15:02 ERROR [order] NullPointerException: cart is null\n"
        "\tat com.acme.order.CartService.total(CartService.java:19)\n"
    )

    def setUp(self):
        self.analyzer = LogAnalyzer()  # 无 API key，走本地分析

    def test_sample_includes_top_frame(self):
        report = self.analyzer.analyze_stdin(self.JAVA)
        joined = "\n".join(report.error_samples)
        self.assertIn("↳", joined, "样本必须带上堆栈顶帧，否则用户不知道在哪儿爆的")
        self.assertIn("CartService.java:19", joined)  # 同类型去重后保留最近一条

    def test_same_type_two_instances_counted_together(self):
        report = self.analyzer.analyze_stdin(self.JAVA)
        self.assertEqual(report.error_count, 2)
        self.assertIn("NullPointerException: 2 个", report.summary)
        self.assertEqual(len(report.error_samples), 1, "同一类型只留一个样本")

    def test_physical_lines_vs_event_count(self):
        report = self.analyzer.analyze_stdin(self.JAVA)
        self.assertEqual(report.analyzed_lines, 5, "分析行数仍是物理行数")
        self.assertEqual(report.event_count, 2, "事件数是归并后的逻辑记录数")

    def test_stack_frames_do_not_inflate_error_count(self):
        """旧实现里每一帧都是 UNKNOWN 行，堆栈越长"分析行数"越假"""
        report = self.analyzer.analyze_stdin(JAVA_STACK_LOG)
        self.assertEqual(report.error_count, 1)
        self.assertEqual(report.event_count, 2)


class TestRootCauseFrame(unittest.TestCase):
    """v0.1.10: 异常链的根因帧 —— 最后一个 Caused by 之后的第一帧才是真凶。

    背调结论：多个 Caused by 时最深层才是根本原因；外层第一帧只是表象。
    旧实现取第一个 at 帧，会指向业务包装层（charge）而非真凶（Driver.load）。
    """

    def setUp(self):
        self.analyzer = LogAnalyzer()

    def test_caused_by_frame_wins_over_outer_frame(self):
        entries = LogParser().parse(CHAINED_CAUSE_LOG)
        frame, is_root = LogAnalyzer._find_frame(entries[0])
        self.assertTrue(is_root)
        self.assertIn("Driver.load(Driver.java:33)", frame)
        self.assertNotIn("PaymentService.charge", frame)

    def test_double_caused_by_uses_last_section(self):
        """两个 Caused by → 取最后一个分节的帧（最深层）"""
        log = (
            "2026-11-12 09:00:00 ERROR [pay] BizException: charge failed\n"
            "\tat com.a.PaymentService.charge(PaymentService.java:10)\n"
            "Caused by: java.sql.SQLException: connection reset\n"
            "\tat com.a.Pool.fetch(Pool.java:20)\n"
            "Caused by: java.net.SocketTimeoutException: Read timed out\n"
            "\tat java.base/java.net.Socket.input(Socket.java:938)\n"
        )
        entries = LogParser().parse(log)
        frame, is_root = LogAnalyzer._find_frame(entries[0])
        self.assertTrue(is_root)
        self.assertIn("Socket.java:938", frame)
        self.assertNotIn("Pool.java:20", frame)

    def test_plain_stack_keeps_first_frame(self):
        """无因果链的普通堆栈：行为与 v0.1.7 一致（第一个帧，非根因标注）"""
        entries = LogParser().parse(JAVA_STACK_LOG)
        frame, is_root = LogAnalyzer._find_frame(entries[0])
        self.assertFalse(is_root)
        self.assertIn("OrderService.java:42", frame)

    def test_chain_tail_without_frame_falls_back(self):
        """链尾只有 "... N more" 没有帧 → 回退到第一个帧，不报错"""
        log = (
            "2026-11-12 09:00:00 ERROR [pay] BizException: charge failed\n"
            "\tat com.a.PaymentService.charge(PaymentService.java:10)\n"
            "Caused by: java.sql.SQLException: same\n"
            "\t... 23 more\n"
        )
        entries = LogParser().parse(log)
        frame, is_root = LogAnalyzer._find_frame(entries[0])
        self.assertFalse(is_root)
        self.assertIn("PaymentService.java:10", frame)

    def test_sample_marks_root_cause(self):
        """报告样本必须显式标注「根因」，用户不用自己往下翻 Caused by"""
        report = self.analyzer.analyze_stdin(CHAINED_CAUSE_LOG)
        joined = "\n".join(report.error_samples)
        self.assertIn("↳ 根因 ", joined)
        self.assertIn("Driver.load(Driver.java:33)", joined)

    def test_python_chained_traceback_root_frame(self):
        """Python 隐式因果链（During handling）→ 取最后分节的 File 帧"""
        log = (
            "2026-11-12 09:00:00,000 - app - ERROR - job failed\n"
            "Traceback (most recent call last):\n"
            '  File "/app/worker.py", line 42, in run\n'
            "    handle(job)\n"
            '  File "/app/db.py", line 9, in handle\n'
            "    conn.query(sql)\n"
            "sqlite3.OperationalError: database is locked\n"
            "\n"
            "During handling of the above exception, another exception occurred:\n"
            "\n"
            "Traceback (most recent call last):\n"
            '  File "/app/retry.py", line 12, in retry\n'
            "    save(state)\n"
            "pickle.PickleError: cannot serialize\n"
        )
        entries = LogParser().parse(log)
        frame, is_root = LogAnalyzer._find_frame(entries[0])
        self.assertTrue(is_root)
        self.assertIn("retry.py", frame)
        self.assertNotIn("worker.py", frame)


class TestUnindentedFrames(unittest.TestCase):
    """v0.1.12: 顶格（无缩进）Java 堆栈帧 —— 自定义 layout 不缩进的场景。

    背调结论（OTel multiline / Fluent Bit / Filebeat / DataSet lineGroupers
    的续行规则全部依赖行首缩进 ^\\s+at）：自定义 log4j/logback layout 顶格
    输出 at 帧时同类工具全部漏合。但顶格判定必须比缩进规则更严格
    （限定符名 + 圆括号），否则英文句子 "at 10 o'clock (see above)"
    会被误并入上一条 —— 宁可拆开，不可粘错的契约继续成立。
    """

    def setUp(self):
        self.parser = LogParser()

    def test_unindented_frames_merge_into_header(self):
        log = (
            "2026-11-14 09:00:00 ERROR [order] NullPointerException: order is null\n"
            "at com.acme.order.OrderService.process(OrderService.java:42)\n"
            "at com.acme.order.OrderController.submit(OrderController.java:88)\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 1, "顶格 at 帧应并入上一条，而不是碎片化")
        self.assertEqual(len(entries[0].stack), 2)
        self.assertIn("OrderService.java:42", entries[0].stack[0])

    def test_unindented_frame_with_jar_suffix(self):
        """Spring Boot logback 帧后缀 ~[classes/:na] 不影响归并"""
        log = (
            "2026-11-14 09:00:00 ERROR [web] ConnectException: refused\n"
            "at org.apache.catalina.core.StandardHostValve.invoke(StandardHostValve.java:139) ~[tomcat-embed-core-10.1.5.jar:10.1.5]\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0].stack), 1)

    def test_unindented_module_path_frame(self):
        """JDK9+ 模块路径帧 java.base/java.lang.Thread.run 也能识别"""
        log = (
            "2026-11-14 09:00:00 ERROR [sys] boom\n"
            "at java.base/java.lang.Thread.run(Thread.java:834)\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0].stack), 1)

    def test_unindented_caused_by_chain(self):
        """顶格 Caused by + 顶格帧的完整因果链一次归并；根因取链尾帧"""
        log = (
            "2026-11-14 09:00:00 ERROR [pay] PaymentException: charge failed\n"
            "at com.acme.pay.PaymentService.charge(PaymentService.java:77)\n"
            "Caused by: java.lang.ClassNotFoundException: no.driver\n"
            "at java.base/java.lang.Class.forName0(Native Method)\n"
            "at com.acme.pay.Driver.load(Driver.java:33)\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0].stack), 4)
        frame, is_root = LogAnalyzer._find_frame(entries[0])
        self.assertTrue(is_root)
        # v0.1.10 口径：最后一个 Caused by 之后的第一帧 = 该分节直接爆点
        self.assertIn("Class.forName0(Native Method)", frame)
        self.assertNotIn("PaymentService.java", frame)

    def test_plain_english_sentence_not_merged(self):
        """反例：英文句子以 at 开头但不是 Java 帧形态，绝不并入"""
        log = (
            "2026-11-14 09:00:00 ERROR [app] batch stalled\n"
            "at 10 o'clock (noon) the queue was still full\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 2, "非帧形态的顶格行是新记录，不得粘上一条")
        self.assertEqual(entries[0].stack, [])

    def test_plain_text_without_parens_not_merged(self):
        """反例：at + 无括号的普通文本不并入"""
        log = (
            "2026-11-14 09:00:00 ERROR [app] rate limit hit\n"
            "at least 5000 requests arrived in one second\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].stack, [])

    def test_unindented_frame_becomes_error_and_top_frame(self):
        """裸异常 + 顶格帧（printStackTrace 直出）：仍推断为 ERROR，顶帧可用"""
        log = (
            "java.lang.IllegalStateException: already closed\n"
            "at com.acme.db.Connection.ensureOpen(Connection.java:58)\n"
            "at com.acme.db.Connection.exec(Connection.java:120)\n"
        )
        entries = self.parser.parse(log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].level, "ERROR")
        frame, _ = LogAnalyzer._find_frame(entries[0])
        self.assertIn("ensureOpen(Connection.java:58)", frame)


if __name__ == "__main__":
    unittest.main()
