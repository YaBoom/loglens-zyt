"""v0.1.4: 解析器最小测试集（零依赖，使用标准库 unittest）。

覆盖各日志格式的解析契约，防止"承诺用法"悄悄腐化。
运行: python -m unittest discover -s tests -v
"""

import unittest

from loglens.parser import LogParser, classify_error


class TestTextFormats(unittest.TestCase):
    """文本日志格式解析"""

    def setUp(self):
        self.parser = LogParser()

    def test_standard_format(self):
        entry = self.parser._parse_line(
            "2026-10-19 10:00:00 ERROR [db] connection timeout"
        )
        self.assertEqual(entry.level, "ERROR")
        self.assertEqual(entry.source, "db")
        self.assertEqual(entry.message, "connection timeout")
        self.assertIsNotNone(entry.timestamp)

    def test_spring_boot_format(self):
        entry = self.parser._parse_line(
            "2026-10-19 10:00:01.123 ERROR 12345 --- [main] c.a.OrderService : NullPointerException"
        )
        self.assertEqual(entry.level, "ERROR")
        self.assertIn("NullPointerException", entry.message)
        self.assertIsNotNone(entry.timestamp)

    def test_python_logging_format(self):
        entry = self.parser._parse_line(
            "2026-10-19 10:00:02,100 - myapp - ERROR - db failed"
        )
        self.assertEqual(entry.level, "ERROR")
        self.assertEqual(entry.message, "db failed")

    def test_unknown_line_falls_back_to_raw(self):
        entry = self.parser._parse_line("some random line without structure")
        self.assertEqual(entry.level, "UNKNOWN")
        self.assertEqual(entry.message, "some random line without structure")

    def test_level_normalization(self):
        self.assertEqual(self.parser._normalize_level("warn"), "WARNING")
        self.assertEqual(self.parser._normalize_level("trace"), "DEBUG")


class TestJsonFormat(unittest.TestCase):
    """v0.1.2 引入的 JSON/JSONL 结构化解析"""

    def setUp(self):
        self.parser = LogParser()

    def test_pure_jsonl(self):
        entry = self.parser._parse_line(
            '{"level":"error","msg":"db connection timeout","ts":"2026-10-19T09:30:01Z","service":"app.db","user_id":42}'
        )
        self.assertEqual(entry.level, "ERROR")
        self.assertEqual(entry.message, "db connection timeout")
        self.assertEqual(entry.source, "app.db")
        self.assertIsNotNone(entry.timestamp)
        self.assertEqual(entry.fields, {"user_id": 42})

    def test_alias_mapping(self):
        """不同日志框架的字段名差异应通过别名抹平"""
        entry = self.parser._parse_line(
            '{"severity":"error","message":"charge failed","logger":"payment"}'
        )
        self.assertEqual(entry.level, "ERROR")
        self.assertEqual(entry.message, "charge failed")
        self.assertEqual(entry.source, "payment")

    def test_prefix_with_embedded_json(self):
        entry = self.parser._parse_line(
            '2026-10-19 09:30:02 ERROR [payment] {"severity":"error","msg":"charge failed","order_id":"A100"}'
        )
        self.assertEqual(entry.level, "ERROR")
        self.assertEqual(entry.message, "charge failed")
        self.assertEqual(entry.fields, {"order_id": "A100"})

    def test_broken_json_falls_back_safely(self):
        """截断/坏 JSON 不得崩溃，回退为 UNKNOWN 原始行"""
        entry = self.parser._parse_line('{"level": "error", "msg": "trunc')
        self.assertIsNotNone(entry)
        self.assertEqual(entry.level, "UNKNOWN")

    def test_non_json_plain_text_has_no_fields(self):
        entry = self.parser._parse_line(
            "2026-10-19 09:30:03 ERROR [auth] plain text failure"
        )
        self.assertIsNone(entry.fields)


class TestParseAndAggregation(unittest.TestCase):
    """整体 parse 与统计聚合"""

    def setUp(self):
        self.parser = LogParser()

    def test_parse_counts_and_skips_blank_lines(self):
        content = (
            "2026-10-19 10:00:00 ERROR [db] timeout\n"
            "\n"
            "2026-10-19 10:00:01 INFO [app] started\n"
        )
        entries = self.parser.parse(content)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].level, "ERROR")
        self.assertEqual(entries[1].level, "INFO")

    def test_filter_by_level(self):
        entries = self.parser.parse(
            "2026-10-19 10:00:00 DEBUG [x] d\n"
            "2026-10-19 10:00:01 INFO [x] i\n"
            "2026-10-19 10:00:02 WARNING [x] w\n"
            "2026-10-19 10:00:03 ERROR [x] e\n"
        )
        self.assertEqual(len(self.parser.filter_by_level(entries, "ERROR")), 1)
        self.assertEqual(len(self.parser.filter_by_level(entries, "WARNING")), 2)
        self.assertEqual(len(self.parser.filter_by_level(entries, "DEBUG")), 4)

    def test_error_summary_counts_types(self):
        entries = self.parser.parse(
            '{"level":"error","msg":"NullPointerException in service"}\n'
            '{"level":"error","msg":"TimeoutError calling api"}\n'
            '{"level":"info","msg":"ok"}\n'
        )
        summary = self.parser.get_error_summary(entries)
        self.assertEqual(summary["total_errors"], 2)
        self.assertEqual(summary["error_types"].get("NullPointerException"), 1)
        self.assertEqual(summary["error_types"].get("TimeoutError"), 1)


class TestErrorClassification(unittest.TestCase):
    """v0.1.6: 错误类型归类（分组）契约。

    核心契约：带可变参数的同类错误必须归到同一类型，
    否则计数会被拆成一堆"各一条"的假类型。
    """

    def test_named_exception_wins(self):
        self.assertEqual(classify_error("NullPointerException: order is null"), "NullPointerException")
        self.assertEqual(classify_error("java.lang.OutOfMemoryError: Java heap space"), "OutOfMemoryError")

    def test_variable_params_do_not_split_types(self):
        """耗时/订单号不同，仍是同一类错误"""
        a = classify_error("ConnectionTimeout: connection timeout after 5000ms")
        b = classify_error("ConnectionTimeout: connection timeout after 5001ms")
        self.assertEqual(a, "ConnectionTimeout")
        self.assertEqual(a, b)

    def test_keyword_types_without_class_name(self):
        cases = {
            "connection timed out after 3000ms": "ConnectionTimeout",
            "read timed out while fetching user 42": "ReadTimeout",
            "Connection refused: dial tcp 10.0.0.1:5432": "ConnectionRefused",
            "Broken pipe while writing response": "ConnectionReset",
            "unknown host api.pay.example.com": "UnknownHost",
            "Transaction rolled back: deadlock detected": "Deadlock",
            "[Errno 28] No space left on device": "DiskFull",
            "open /etc/app.conf: permission denied": "PermissionDenied",
            "upstream 429: too many requests": "RateLimited",
            "request timeout": "Timeout",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_error(message), expected)

    def test_generic_fallback(self):
        self.assertEqual(classify_error("something went wrong in handler"), "GenericError")
        self.assertEqual(classify_error(""), "Unknown")

    def test_lowercase_error_word_is_not_a_type(self):
        """"error" 普通单词不能被当成 XxxError 类型"""
        self.assertEqual(classify_error("failed to send error report"), "GenericError")

    def test_error_summary_groups_variable_messages(self):
        entries = LogParser().parse(
            "2026-10-19 10:00:00 ERROR [db] connection timeout after 5000ms\n"
            "2026-10-19 10:00:01 ERROR [db] connection timeout after 8000ms\n"
            "2026-10-19 10:00:02 ERROR [db] connection timed out after 9000ms\n"
        )
        summary = LogParser().get_error_summary(entries)
        self.assertEqual(summary["total_errors"], 3)
        self.assertEqual(summary["error_types"], {"ConnectionTimeout": 3})


if __name__ == "__main__":
    unittest.main()
