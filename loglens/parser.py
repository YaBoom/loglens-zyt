"""
日志解析器 - 解析各种格式的日志
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


# v0.1.6: 命名异常/错误类（NullPointerException / TimeoutError ...）
# 只认"首字母大写 + Exception/Error 结尾"的标识符，避免把普通单词 error 当成类型。
_NAMED_ERROR_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Exception|Error))\b")

# v0.1.7: 多行堆栈的"续行"识别。
# 背调结论（Alibaba Logtail / Grafana Alloy / OneUptime / Dataset 文档一致）：
# 采集器按行切分会把一条 Java 异常拆成 6~40 条无上下文记录，堆栈帧再也无法与
# 触发它的异常关联——这是同类工具公认的头号痛点。
#
# 但这里刻意"反向定义"：不去描述堆栈长什么样，而是**只把明确像续行的行并入上一条**，
# 其余一律当作新记录。原因是合并过宽比没合更危险——粘错会让两条独立日志变一条，
# 错误计数静默变少，比刷屏更难发现。
_CONTINUATION_RE = re.compile(
    r"^(?:"
    r"[ \t]+at[ \t]+\S+"                 # \tat com.x.Y.m(Y.java:10) / \tat module.func
    r"|[ \t]{2,}\S"                      # 通用缩进续行（Python 源码行、折行消息）
    r"|Caused by:"                       # Java 因果链
    r"|Suppressed:"                      # Java suppressed 异常
    r"|[ \t]*\.\.\.\s*\d+\s+more[ \t]*$" # Java ... 23 more
    r"|[ \t]*File\s+\".+\",\s*line\s+\d+"# Python 堆栈帧
    r"|During handling of the above exception"      # Python 隐式因果链分节标记
    r"|The above exception was the direct cause"    # Python 显式 raise ... from
    r")"
)

# Python 因果链分节标记（顶格出现，且常在上一节异常行 flush 之后到达——
# 那时 header 已清空，需要"重新接回上一条记录"而不是独立成条）
_PY_CHAIN_RE = re.compile(
    r"^(?:During handling of the above exception|The above exception was the direct cause)"
)

# Python traceback 的首行只是个标题，真正的异常在其后顶格那一行
_TRACEBACK_START_RE = re.compile(r"Traceback\s*\(most recent call last\)")


# v0.1.6: 无语义化类名的错误（"connection timed out after 5000ms"）按关键词归类。
# 顺序敏感：越具体越靠前，兜底的 Timeout 必须放最后，否则会吞掉 ConnectionTimeout/ReadTimeout。
# 归类目标是与 Java/Python 异常类同构的"可读类型名"，让计数和样本去重共用同一口径。
_KEYWORD_ERROR_RULES = [
    ("OutOfMemoryError", re.compile(
        r"out\s*of\s*memory|\boom\b|cannot allocate memory|memory exhausted", re.I)),
    ("ConnectionTimeout", re.compile(
        r"connection\s*time[ds]?\s*out|connectiontimeout|connect\s*time[ds]?\s*out", re.I)),
    ("ReadTimeout", re.compile(
        r"(?:read|socket)\s*time[ds]?\s*out|readtimeout|sockettimeout", re.I)),
    ("ConnectionRefused", re.compile(r"connection\s*refused|econnrefused", re.I)),
    ("ConnectionReset", re.compile(r"connection\s*reset|broken\s*pipe|econnreset", re.I)),
    ("UnknownHost", re.compile(
        r"unknown\s*host|no\s*such\s*host|name\s*or\s*service\s*not\s*known|"
        r"temporary\s*failure\s*in\s*name\s*resolution", re.I)),
    ("Deadlock", re.compile(r"dead\s*lock", re.I)),
    ("DiskFull", re.compile(r"no\s*space\s*left|disk\s*(?:is\s*)?full|enospc", re.I)),
    ("PermissionDenied", re.compile(r"permission\s*denied|access\s*is\s*denied|eacces", re.I)),
    ("AuthFailed", re.compile(
        r"auth(?:entication|orization)?\s*(?:failed|failure)|invalid\s*(?:token|credentials?)", re.I)),
    ("RateLimited", re.compile(r"rate\s*limit(?:ed)?|too\s*many\s*requests", re.I)),
    ("Timeout", re.compile(r"time[ds]?\s*out|timedout", re.I)),
]


def classify_error(message: str) -> str:
    """v0.1.6: 把任意错误消息归类成一个稳定的"错误类型"。

    用户痛点（Sentry 官方多年投入的核心问题）：同类错误因为消息里带
    可变参数（耗时 5000ms / 5001ms、订单号、IP）被拆成很多类，
    计数看着一堆类型，实际只有一个问题——分组的价值大于计数。

    策略（保守优先，宁可少合并也不要错合并）：
    1. 有显式异常类名（XxxException / XxxError）直接用类名；
    2. 否则按关键词规则表归类（超时/连接拒绝/OOM/磁盘满/权限...）；
    3. 都不命中才回落 GenericError。
    """
    if not message:
        return "Unknown"

    match = _NAMED_ERROR_RE.search(message)
    if match:
        name = match.group(1)
        # "Error"/"Exception" 单独出现没有信息量，交给关键词规则判断
        if name not in ("Error", "Exception"):
            return name

    for type_name, pattern in _KEYWORD_ERROR_RULES:
        if pattern.search(message):
            return type_name

    return "GenericError"


@dataclass
class LogEntry:
    """日志条目"""
    timestamp: Optional[datetime]
    level: str
    message: str
    source: Optional[str]
    raw: str
    # v0.1.2: JSON/JSONL 结构化日志附带的字段字典
    fields: Optional[dict] = field(default=None)
    # v0.1.7: 归并进来的续行（堆栈帧 / Caused by / Python traceback 帧）
    stack: List[str] = field(default_factory=list)


class LogParser:
    """日志解析器"""
    
    # 支持的日志格式
    PATTERNS = {
        # 标准格式: 2026-03-16 10:30:45 ERROR [source] message
        # v0.1.9: 补 CRITICAL —— Python logging/logging4j 常见级别，漏掉会整行降级 UNKNOWN
        "standard": r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL|TRACE)\s+\[?([^\]]+)?\]?\s+(.*)",
        
        # Spring Boot 格式
        # v0.1.4: logger 与冒号之间允许空格 — 真实输出形如
        # "2026-10-19 10:00:01.123 ERROR 12345 --- [main] c.a.OrderService : NullPointerException"
        # 旧正则写死 "(\S+):"，遇空格即不匹配，整行降级为 UNKNOWN
        "spring": r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(\w+)\s+(\S+)\s+---\s+\[\s*(\S+)\]\s+(\S+)\s*:\s+(.*)",
        
        # JSON 格式 (部分匹配)
        "json": r'"(timestamp|level|message)":\s*"([^"]+)"',
        
        # Python 格式
        "python": r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\s+-\s+(\S+)\s+-\s+(\S+)\s+-\s+(.*)",
    }
    
    LEVEL_KEYWORDS = {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "WARN": "WARNING",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "FATAL": "FATAL",
        "CRITICAL": "CRITICAL",
        "TRACE": "DEBUG",
    }

    # v0.1.9: 级别排序表。此前藏在 filter_by_level 局部变量里且缺 WARN/CRITICAL，
    # 导致 --level WARN 静默降级为 INFO、CRITICAL 日志在默认 ERROR 阈值下被静默排除
    LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "FATAL": 4, "CRITICAL": 4}

    # v0.1.2: JSON 日志常见字段别名（不同日志框架字段名差异大）
    JSON_FIELD_ALIASES = {
        "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "date"),
        "level": ("level", "severity", "lvl", "sev", "loglevel", "log_level"),
        "message": ("message", "msg", "event", "text", "log", "content"),
        "source": ("logger", "source", "component", "service", "app", "module", "name"),
    }
    
    def __init__(self):
        self.compiled_patterns = {
            name: re.compile(pattern) 
            for name, pattern in self.PATTERNS.items()
        }
    
    def parse(self, content: str) -> List[LogEntry]:
        """解析日志内容

        v0.1.7: 先按"新事件起始"把物理行归并成逻辑记录，再解析表头。
        一条 Java 异常连同它的 30 行堆栈 = 1 条记录，而不是 30 条无上下文的碎片。
        """
        entries = []
        for header, continuation, tb_exception in self._group_records(content.split("\n")):
            entry = self._parse_record(header, continuation, tb_exception)
            if entry:
                entries.append(entry)
        return entries

    def _group_records(self, lines: List[str]):
        """v0.1.7: 把物理行归并成 (表头行, 续行列表, Python异常行) 三元组。

        判定顺序（保守优先）：
        1. 行本身是日志格式的起始行 → 一定是新记录，绝不并入上一条；
        2. 行明确像续行（缩进 at/File、Caused by、... N more）→ 并入当前记录；
        3. 当前记录是 Python traceback 且尚未收到异常行 → 该顶格行就是异常本身；
        4. 其余 → 独立成一条新记录（宁可拆开，不可粘错）。

        空行被跳过而不是用来截断记录 —— Java 常在 Caused by 前留空行，
        用空行断组会把因果链拦腰切断。
        """
        records = []
        header: Optional[str] = None
        continuation: List[str] = []
        tb_mode = False
        tb_exception: Optional[str] = None

        def flush():
            nonlocal header, continuation, tb_mode, tb_exception
            if header is not None:
                records.append((header, continuation, tb_exception))
            header, continuation, tb_mode, tb_exception = None, [], False, None

        for raw in lines:
            line = raw.rstrip("\r")
            if not line.strip():
                continue

            if header is None:
                # v0.1.10: Python 因果链分节标记（"During handling..." 等）出现在
                # 上一节异常行 flush 之后（header 已清空）。它明确宣告"这还是同一次
                # 故障"，应重新接回上一条记录 —— 否则一次链式故障会被算成多条错误，
                # 根因帧也会与原始异常失去关联。
                if records and _PY_CHAIN_RE.match(line):
                    prev_header, prev_cont, prev_tb = records.pop()
                    header = prev_header
                    # 上一节的异常行回填到标记之前（保持原始顺序），标记行本身也入续行
                    continuation = prev_cont + ([prev_tb] if prev_tb else []) + [line]
                    tb_exception = None  # 已回填，等新分节的异常行重新捕获
                    tb_mode = False      # 等真正的 "Traceback..." 行到了再开捕获
                    continue
                header = line
                tb_mode = bool(_TRACEBACK_START_RE.search(line))
                continue

            if _TRACEBACK_START_RE.search(line):
                # Python 的 traceback 是紧跟在 ERROR 行后面逐行打印的，
                # 它属于上面那条记录 —— 若当成新记录，一次故障会被算成两条错误。
                continuation.append(line)
                tb_mode = True
                continue

            if self._is_continuation(line):
                continuation.append(line)
                continue

            if tb_mode and tb_exception is None and continuation:
                # "Traceback..." 之后顶格的第一行是异常本身（ValueError: xxx），
                # 它是记录的结尾而不是新记录。
                tb_exception = line
                flush()
                continue

            flush()
            header = line
            tb_mode = bool(_TRACEBACK_START_RE.search(line))

        flush()
        return records

    def _is_continuation(self, line: str) -> bool:
        """该行是否属于上一条记录的续行（堆栈帧等）"""
        if self._looks_like_record_start(line):
            return False
        return bool(_CONTINUATION_RE.match(line))

    def _looks_like_record_start(self, line: str) -> bool:
        """行本身是否标志着一条新日志的开始（即使它带缩进）"""
        stripped = line.lstrip()
        if not stripped:
            return False
        for name in ("standard", "spring", "python"):
            if self.compiled_patterns[name].match(stripped):
                return True
        return stripped.startswith("{") and stripped.endswith("}")

    def _parse_record(
        self,
        header: str,
        continuation: List[str],
        tb_exception: Optional[str] = None,
    ) -> Optional[LogEntry]:
        """v0.1.7: 解析一条（可能多行的）逻辑记录"""
        # 表头可能带缩进（v0.1.7 允许缩进的格式起始行独立成条），
        # 但格式正则都按行首锚定，所以匹配前先去掉缩进。
        entry = self._parse_line(header.lstrip())
        if entry is None:
            return None

        entry.raw = "\n".join([header] + continuation + ([tb_exception] if tb_exception else []))

        if tb_exception is not None:
            # Python traceback：异常行才是机器可读的类型来源（ValueError/KeyError...），
            # 而前面的业务消息（"job crashed"）对人有用，一条记录里都留下。
            exception = tb_exception.strip()
            prior = entry.message.strip()
            if _TRACEBACK_START_RE.search(header):
                # 记录本身就是 traceback（日志从堆栈开始），异常行即报错内容
                entry.message = exception
                entry.stack = [header] + continuation
            else:
                entry.message = f"{prior} ← {exception}" if prior else exception
                entry.stack = continuation  # 首元素是 "Traceback..."，顶帧查找会跳过它
            if entry.level == "UNKNOWN":
                entry.level = "ERROR"  # traceback 本身就是错误，无需再猜
            return entry

        entry.stack = continuation
        if continuation and entry.level == "UNKNOWN" and classify_error(header) != "GenericError":
            # printStackTrace() 直出的裸异常行没有级别标记，但有堆栈 + 可识别类型，
            # 认定它是错误行有据可依（否则整段堆栈在报告里完全不可见）。
            entry.level = "ERROR"
        return entry
    
    def _parse_line(self, line: str) -> Optional[LogEntry]:
        """解析单行日志"""
        # v0.1.2: 优先尝试 JSON/JSONL 结构化解析（纯 JSON 行或前缀 + 嵌入 JSON 对象）
        json_entry = self._try_parse_json(line)
        if json_entry:
            return json_entry

        # 尝试标准格式
        match = self.compiled_patterns["standard"].match(line)
        if match:
            timestamp_str, level, source, message = match.groups()
            return LogEntry(
                timestamp=self._parse_timestamp(timestamp_str),
                level=self._normalize_level(level),
                message=message,
                source=source,
                raw=line
            )
        
        # 尝试 Spring Boot 格式
        match = self.compiled_patterns["spring"].match(line)
        if match:
            timestamp_str, level, thread, source, logger, message = match.groups()
            return LogEntry(
                timestamp=self._parse_timestamp(timestamp_str),
                level=self._normalize_level(level),
                message=message,
                source=source,
                raw=line
            )
        
        # 尝试 Python 格式
        match = self.compiled_patterns["python"].match(line)
        if match:
            timestamp_str, logger, level, message = match.groups()
            return LogEntry(
                timestamp=self._parse_timestamp(timestamp_str),
                level=self._normalize_level(level),
                message=message,
                source=logger,
                raw=line
            )
        
        # 无法解析，返回原始行
        return LogEntry(
            timestamp=None,
            level="UNKNOWN",
            message=line,
            source=None,
            raw=line
        )
    
    def _try_parse_json(self, line: str) -> Optional[LogEntry]:
        """v0.1.2: 尝试将一行解析为 JSON 日志。

        支持两种形态:
        1. 纯 JSON 行 (JSONL): {"level":"error","msg":"db timeout"}
        2. 前缀 + 嵌入 JSON 对象: 2026-10-15 10:00:00 ERROR {"level":"error","msg":"..."}
        解析失败（非 JSON / 截断 JSON）则返回 None，回退到文本格式解析。
        """
        if "{" not in line or "}" not in line:
            return None

        # 候选区间: 第一个 "{" 到最后一个 "}"
        candidate = line[line.index("{"): line.rindex("}") + 1]
        if not candidate.strip():
            return None

        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        # 按别名映射提取常见字段，剩余字段原样保留
        fields = dict(data)
        timestamp = None
        level = "UNKNOWN"
        message = ""
        source = None

        ts_val = self._pop_alias(fields, "timestamp")
        if isinstance(ts_val, str):
            timestamp = self._parse_timestamp(ts_val.replace("T", " ").replace("Z", ""))

        level_val = self._pop_alias(fields, "level")
        if isinstance(level_val, str) and level_val:
            level = self._normalize_level(level_val)

        msg_val = self._pop_alias(fields, "message")
        if msg_val is not None:
            message = str(msg_val)

        source_val = self._pop_alias(fields, "source")
        if isinstance(source_val, str):
            source = source_val

        # 嵌入 JSON 的行可能还有前缀（如 "10:00:00 ERROR {...}"），
        # 消息为空时用前缀文本补充
        if not message:
            prefix = line[: line.index("{")].strip()
            if prefix:
                message = prefix
            else:
                message = line

        return LogEntry(
            timestamp=timestamp,
            level=level,
            message=message,
            source=source,
            raw=line,
            fields=fields or None,
        )

    def _pop_alias(self, fields: dict, category: str):
        """按类别从字段字典中弹出首个命中的别名字段"""
        for alias in self.JSON_FIELD_ALIASES[category]:
            if alias in fields:
                return fields.pop(alias)
        return None

    def _parse_timestamp(self, timestamp_str: str) -> Optional[datetime]:
        """解析时间戳"""
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(timestamp_str.replace("T", " ").replace("Z", ""), fmt)
            except ValueError:
                continue
        return None
    
    def _normalize_level(self, level: str) -> str:
        """标准化日志级别"""
        return self.LEVEL_KEYWORDS.get(level.upper(), "INFO")
    
    def filter_by_level(self, entries: List[LogEntry], min_level: str = "INFO") -> List[LogEntry]:
        """按级别过滤（v0.1.9: 阈值别名先归一，未知级别显式报错，不再静默降级）"""
        canonical = self.LEVEL_KEYWORDS.get(str(min_level).upper())
        if canonical is None or canonical not in self.LEVEL_ORDER:
            raise ValueError(
                f"未知日志级别: {min_level!r}（可选: DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL）"
            )
        min_level_value = self.LEVEL_ORDER[canonical]
        return [
            e for e in entries
            if self.LEVEL_ORDER.get(e.level, 1) >= min_level_value
        ]
    
    def get_error_summary(self, entries: List[LogEntry]) -> dict:
        """获取错误摘要"""
        errors = [e for e in entries if e.level in ("ERROR", "FATAL", "CRITICAL")]
        
        # 统计错误类型（v0.1.6: 统一走 classify_error，识别无语义类名的错误）
        error_types = {}
        for error in errors:
            error_type = classify_error(error.message)
            error_types[error_type] = error_types.get(error_type, 0) + 1
        
        return {
            "total_errors": len(errors),
            "error_types": error_types,
            "errors": errors[-10:]  # 返回最近10个错误
        }
