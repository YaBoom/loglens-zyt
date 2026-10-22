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


class LogParser:
    """日志解析器"""
    
    # 支持的日志格式
    PATTERNS = {
        # 标准格式: 2026-03-16 10:30:45 ERROR [source] message
        "standard": r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(DEBUG|INFO|WARN|WARNING|ERROR|FATAL|TRACE)\s+\[?([^\]]+)?\]?\s+(.*)",
        
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
        """解析日志内容"""
        entries = []
        for line in content.strip().split("\n"):
            if not line.strip():
                continue
            entry = self._parse_line(line)
            if entry:
                entries.append(entry)
        return entries
    
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
        """按级别过滤"""
        level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "FATAL": 4}
        min_level_value = level_order.get(min_level.upper(), 1)
        return [
            e for e in entries 
            if level_order.get(e.level, 1) >= min_level_value
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
