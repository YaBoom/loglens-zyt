"""
日志分析器 - 核心分析逻辑
"""

import os
import sys
from dataclasses import dataclass
from typing import Optional, List
from .parser import LogParser, LogEntry
from .llm_client import get_llm_client, LLMClient


@dataclass
class AnalysisReport:
    """分析报告"""
    summary: str
    root_cause: str
    suggestions: List[str]
    error_count: int
    warning_count: int
    analyzed_lines: int


class LogAnalyzer:
    """日志分析器"""

    def __init__(self, model_provider: str = "openai", api_key: str = None, debug: bool = False):
        self.parser = LogParser()
        self.model_provider = model_provider
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.debug = debug or os.environ.get("LOGLENS_DEBUG", "").lower() in ("1", "true", "yes")
        self._llm_client: Optional[LLMClient] = None

    def _debug_print(self, message: str) -> None:
        """仅在 debug 模式下输出调试信息，避免污染正常输出"""
        if self.debug:
            print(f"[DEBUG] {message}")

    @property
    def llm_client(self) -> Optional[LLMClient]:
        """获取LLM客户端"""
        if self.api_key and not self._llm_client:
            self._llm_client = get_llm_client(self.api_key)
        return self._llm_client
    
    def analyze(self, log_path: str, tail: Optional[int] = None, min_level: str = "ERROR") -> AnalysisReport:
        """分析日志文件"""
        
        # 读取日志内容
        if log_path == "-":
            # 从 stdin 读取（支持管道: grep ERROR app.log | loglens analyze -）
            content = sys.stdin.read()
        elif tail:
            # 读取最后 tail 行
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                content = "".join(lines[-tail:])
        else:
            # 读取全部
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        
        # 解析日志
        entries = self.parser.parse(content)
        
        # 过滤错误级别
        filtered_entries = self.parser.filter_by_level(entries, min_level)
        
        # 统计分析
        error_summary = self.parser.get_error_summary(filtered_entries)
        
        # AI 分析根因
        root_cause, suggestions = self._analyze_with_ai(entries, error_summary)
        
        # 生成摘要
        summary = self._generate_summary(error_summary)
        
        return AnalysisReport(
            summary=summary,
            root_cause=root_cause,
            suggestions=suggestions,
            error_count=error_summary["total_errors"],
            warning_count=len([e for e in filtered_entries if e.level == "WARNING"]),
            analyzed_lines=len(entries)
        )
    
    def _generate_summary(self, error_summary: dict) -> str:
        """生成摘要"""
        total = error_summary["total_errors"]
        if total == 0:
            return "✅ 未检测到错误日志"
        
        lines = [f"📊 检测到 {total} 个错误:"]
        for error_type, count in error_summary["error_types"].items():
            lines.append(f"  - {error_type}: {count} 个")
        
        return "\n".join(lines)
    
    def _analyze_with_ai(self, entries: List[LogEntry], error_summary: dict) -> tuple:
        """使用AI分析根因"""

        # 提取关键错误信息
        error_messages = []
        for error in error_summary.get("errors", [])[:5]:
            error_messages.append(error.message[:200])  # 限制长度

        # 构建提示词
        prompt = self._build_prompt(error_messages)

        # 调试信息（仅 debug 模式输出）
        self._debug_print(f"API Key已设置: {bool(self.api_key)}")
        self._debug_print(f"LLM Client已初始化: {self.llm_client is not None}")
        self._debug_print(f"错误消息数量: {len(error_messages)}")

        # 使用真实 LLM 分析
        if self.llm_client and error_messages:
            try:
                self._debug_print("正在调用 DeepSeek LLM...")
                result = self.llm_client.analyze_log_error(
                    error_messages,
                    error_summary.get("error_types", {})
                )
                self._debug_print("LLM 调用成功!")
                root_cause = result["root_cause"]
                suggestions = result["suggestions"]
            except Exception as e:
                # LLM调用失败时回退到本地分析
                print(f"⚠️ LLM调用失败，回退到本地分析: {e}")
                root_cause = self._local_analysis(error_summary)
                suggestions = self._generate_suggestions(error_summary)
        else:
            # 没有API key时使用本地分析
            if not self.api_key:
                self._debug_print("未设置API Key，使用本地分析")
            if not error_messages:
                self._debug_print("没有错误信息，使用本地分析")
            root_cause = self._local_analysis(error_summary)
            suggestions = self._generate_suggestions(error_summary)

        return root_cause, suggestions
    
    def _build_prompt(self, error_messages: List[str]) -> str:
        """构建分析提示词"""
        return f"""请分析以下日志中的错误信息，找出根因并给出修复建议：

错误信息:
{chr(10).join(error_messages)}

请分析：
1. 根因是什么？
2. 需要如何修复？
3. 需要注意什么？"""
    
    def _local_analysis(self, error_summary: dict) -> str:
        """本地分析根因（无API key时的备用方案）"""

        error_types = error_summary.get("error_types", {})
        
        # 基于错误类型推断根因
        if "NullPointerException" in error_types or "NullPointerException" in str(error_types):
            return "检测到空指针异常，可能是因为对象未初始化或API返回null未做判空处理"
        
        elif "ConnectionTimeout" in error_types or "ConnectionTimeout" in str(error_types):
            return "数据库/服务连接超时，可能原因：连接池耗尽、网络延迟、服务端负载过高"
        
        elif "OutOfMemoryError" in error_types or "MemoryError" in str(error_types):
            return "内存溢出，可能原因：内存泄漏、大对象加载、并发过高"
        
        elif "SQLException" in error_types or "SQLError" in str(error_types):
            return "数据库异常，可能原因：SQL语法错误、连接问题、死锁"
        
        elif "TimeoutException" in error_types:
            return "操作超时，可能原因：网络不稳定、服务响应慢、并发阻塞"
        
        else:
            return "从日志中检测到异常，建议查看具体错误堆栈信息进行进一步分析"
    
    def _generate_suggestions(self, error_summary: dict) -> List[str]:
        """生成修复建议"""
        error_types = error_summary.get("error_types", {})
        suggestions = []
        
        if "NullPointerException" in error_types or "NullPointerException" in str(error_types):
            suggestions.extend([
                "1. 增加空指针检查，使用 Optional 或 null 判空",
                "2. 在API调用后添加默认值处理",
                "3. 使用 @NonNull 注解强制检查"
            ])
        
        if "ConnectionTimeout" in error_types or "ConnectionTimeout" in str(error_types):
            suggestions.extend([
                "1. 增加连接池大小配置",
                "2. 添加连接超时和读超时配置",
                "3. 优化慢查询，添加适当索引",
                "4. 实现重试机制和熔断"
            ])
        
        if "OutOfMemoryError" in error_types or "MemoryError" in str(error_types):
            suggestions.extend([
                "1. 检查是否有内存泄漏，使用 MAT 工具分析",
                "2. 调整 JVM 堆内存大小",
                "3. 优化大对象处理，使用流式处理",
                "4. 限制并发请求数"
            ])
        
        if "SQLException" in error_types or "SQLError" in str(error_types):
            suggestions.extend([
                "1. 检查SQL语法和参数",
                "2. 添加连接池监控",
                "3. 优化查询语句，添加索引",
                "4. 检查是否有死锁"
            ])
        
        # 如果没有特定建议
        if not suggestions:
            suggestions = [
                "1. 查看完整错误堆栈定位具体问题",
                "2. 搜索相关错误信息",
                "3. 检查最近代码改动",
                "4. 开启详细日志进行排查"
            ]
        
        return suggestions
    
    def analyze_stdin(self, content: str, min_level: str = "ERROR") -> AnalysisReport:
        """从标准输入分析日志"""
        
        # 解析日志
        entries = self.parser.parse(content)
        
        # 过滤错误级别
        filtered_entries = self.parser.filter_by_level(entries, min_level)
        
        # 统计分析
        error_summary = self.parser.get_error_summary(filtered_entries)
        
        # AI 分析根因
        root_cause, suggestions = self._analyze_with_ai(entries, error_summary)
        
        # 生成摘要
        summary = self._generate_summary(error_summary)
        
        return AnalysisReport(
            summary=summary,
            root_cause=root_cause,
            suggestions=suggestions,
            error_count=error_summary["total_errors"],
            warning_count=len([e for e in filtered_entries if e.level == "WARNING"]),
            analyzed_lines=len(entries)
        )
