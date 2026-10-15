"""
日志解析器 - 解析各种格式的日志
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


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
        "spring": r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(\w+)\s+(\S+)\s+---\s+\[\s*(\S+)\]\s+(\S+):\s+(.*)",
        
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
        
        # 统计错误类型
        error_types = {}
        for error in errors:
            # 提取错误类型
            if "Exception" in error.message:
                match = re.search(r"(\w+Exception)", error.message)
                if match:
                    error_type = match.group(1)
                else:
                    error_type = "Unknown"
            elif "Error" in error.message:
                match = re.search(r"(\w+Error)", error.message)
                if match:
                    error_type = match.group(1)
                else:
                    error_type = "Error"
            else:
                error_type = "GenericError"
            
            error_types[error_type] = error_types.get(error_type, 0) + 1
        
        return {
            "total_errors": len(errors),
            "error_types": error_types,
            "errors": errors[-10:]  # 返回最近10个错误
        }
