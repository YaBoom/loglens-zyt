"""
日志分析器 - 核心分析逻辑
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Optional, List
from .parser import LogParser, LogEntry, classify_error
from .llm_client import get_llm_client, LLMClient


# v0.1.6: 本地分析知识表。键必须与 parser.classify_error() 产出的类型名一致，
# 否则分类改对了、结论还查不到表 —— 分类与结论共用一套词汇才算打通。
ROOT_CAUSE_HINTS = {
    "NullPointerException": "空指针异常，可能是因为对象未初始化或 API 返回 null 未做判空处理",
    "ConnectionTimeout": "连接超时，可能原因：连接池耗尽、网络延迟、服务端负载过高",
    "ReadTimeout": "读超时，可能原因：下游响应慢、返回数据量过大、网络抖动",
    "Timeout": "操作超时，可能原因：网络不稳定、服务响应慢、并发阻塞",
    "ConnectionRefused": "连接被拒绝，通常是对端服务未启动、端口不对或被防火墙拦截",
    "ConnectionReset": "连接被对端重置或管道中断，通常是下游进程重启、提前关闭连接",
    "UnknownHost": "域名解析失败，可能原因：DNS 配置错误、服务名拼写错误、容器网络不通",
    "Deadlock": "检测到死锁，通常是多事务加锁顺序不一致导致循环等待",
    "DiskFull": "磁盘空间不足，需清理日志/临时文件或扩容，否则写入会持续失败",
    "PermissionDenied": "权限不足，通常是文件属主或运行账户权限配置问题",
    "AuthFailed": "认证失败，可能是凭证过期、密钥错误或权限被回收",
    "RateLimited": "被上游限流，需检查调用频率、配额与退避重试策略",
    "OutOfMemoryError": "内存溢出，可能原因：内存泄漏、大对象加载、并发过高",
    "SQLException": "数据库异常，可能原因：SQL 语法错误、连接问题、死锁",
}

SUGGESTION_HINTS = {
    "NullPointerException": [
        "增加空指针检查，使用 Optional 或 null 判空",
        "在 API 调用后添加默认值处理",
        "使用 @NonNull 注解强制检查",
    ],
    "ConnectionTimeout": [
        "增加连接池大小配置",
        "添加连接超时和读超时配置",
        "优化慢查询，添加适当索引",
        "实现重试机制和熔断",
    ],
    "ReadTimeout": [
        "为下游调用设置合理的读超时并加退避重试",
        "分页/流式拉取大结果集，避免一次性读取",
        "排查下游服务在高峰时段的响应时间",
    ],
    "Timeout": [
        "定位超时链路上最慢的一段（DB/HTTP/锁等待）",
        "为关键调用设置超时与熔断，避免线程堆积",
        "检查是否存在并发阻塞或长事务",
    ],
    "ConnectionRefused": [
        "确认对端服务已启动且端口正确",
        "检查防火墙/安全组与容器网络策略",
        "确认连接串中的 host:port 配置无误",
    ],
    "ConnectionReset": [
        "确认下游服务是否重启或存在 OOM 被 kill",
        "检查 keep-alive 空闲超时是否短于客户端复用时间",
        "为幂等请求增加重试",
    ],
    "UnknownHost": [
        "核对域名/服务名拼写与 DNS 配置",
        "在容器内验证 DNS 解析（nslookup/getent hosts）",
        "为关键依赖配置本地方案或就近接入点",
    ],
    "Deadlock": [
        "统一多表/多资源的加锁顺序",
        "缩短事务范围，避免事务内做远程调用",
        "为死锁重试加捕获与退避",
    ],
    "DiskFull": [
        "清理历史日志并配置 logrotate 轮转与保留期",
        "对日志/临时目录做容量监控与告警",
        "必要时扩容磁盘或迁移数据目录",
    ],
    "PermissionDenied": [
        "确认运行账户对目标文件/目录的属主与权限",
        "检查容器挂载目录的权限继承",
        "避免使用 root 运行以定位真实权限问题",
    ],
    "AuthFailed": [
        "检查凭证/Token 是否过期或被轮换",
        "确认签名、时间戳与时钟偏移",
        "为认证失败补充可区分的错误码便于定位",
    ],
    "RateLimited": [
        "按上游配额做限流与排队，避免突发打满",
        "实现指数退避重试并尊重 Retry-After",
        "申请更高配额或做请求合并",
    ],
    "OutOfMemoryError": [
        "检查是否有内存泄漏，使用 MAT 等工具分析堆转储",
        "调整 JVM 堆内存大小",
        "优化大对象处理，使用流式处理",
        "限制并发请求数",
    ],
    "SQLException": [
        "检查 SQL 语法和参数绑定",
        "添加连接池监控，确认连接是否泄漏",
        "优化查询语句，添加索引",
        "检查是否有死锁或长事务",
    ],
}

GENERIC_ROOT_CAUSE = "从日志中检测到异常，建议查看具体错误堆栈信息进行进一步分析"
GENERIC_SUGGESTIONS = [
    "查看完整错误堆栈定位具体问题",
    "搜索相关错误信息",
    "检查最近代码改动",
    "开启详细日志进行排查",
]


@dataclass
class AnalysisReport:
    """分析报告"""
    summary: str
    root_cause: str
    suggestions: List[str]
    error_count: int
    warning_count: int
    analyzed_lines: int
    # v0.1.5: 去重后的原始错误行样本（报告可读性：只看计数看不到具体报错内容）
    error_samples: List[str] = field(default_factory=list)


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
            analyzed_lines=len(entries),
            error_samples=self._collect_error_samples(error_summary)
        )
    
    def _collect_error_samples(self, error_summary: dict, limit: int = 5, max_len: int = 300) -> List[str]:
        """v0.1.5: 收集去重后的原始错误行样本。

        用户痛点: 报告此前只有计数和根因推断，看不到具体错误内容，
        无法确认根因判断是否对得上自己的日志。这里从 error_summary
        中取最近的错误条目，按错误类型去重（每类保留最新一条），
        每行截断到 max_len 防止超长堆栈刷屏。
        """
        samples: List[str] = []
        seen_types = set()
        for error in reversed(error_summary.get("errors", [])):
            # v0.1.6: 复用 parser 的分类口径做去重键。
            # 之前这里内联了一份"有没有 Exception/Error"的判断，与 get_error_summary
            # 各写一遍，改了一处忘另一处就会"计数说 1 类、样本去重成 5 条"。
            key = classify_error(error.message)
            if key in seen_types:
                continue
            seen_types.add(key)
            raw = error.raw.strip()
            if len(raw) > max_len:
                raw = raw[:max_len] + "…"
            samples.append(raw)
            if len(samples) >= limit:
                break
        return samples

    def _generate_summary(self, error_summary: dict) -> str:
        """生成摘要"""
        total = error_summary["total_errors"]
        if total == 0:
            return "✅ 未检测到错误日志"
        
        lines = [f"📊 检测到 {total} 个错误:"]
        # v0.1.6: 按数量降序，先让用户看到"主要矛盾"（最高频的那类错误）
        for error_type, count in sorted(
            error_summary["error_types"].items(), key=lambda kv: (-kv[1], kv[0])
        ):
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
        """本地分析根因（无API key时的备用方案）

        v0.1.6: 改成"按最高频错误类型查表"。旧实现是一串 if/elif，
        命中顺序决定结论（NullPointerException 永远赢），于是出现
        「摘要说 ConnectionTimeout 最多、根因却说空指针」的自相矛盾。
        现在结论显式声明依据（类型 + 次数），用户能判断该不该信。
        """
        top_types = self._top_error_types(error_summary, limit=1)
        if not top_types:
            return GENERIC_ROOT_CAUSE

        top_type = top_types[0]
        count = error_summary.get("error_types", {}).get(top_type, 0)
        hint = ROOT_CAUSE_HINTS.get(top_type)
        if hint is None:
            hint = (
                GENERIC_ROOT_CAUSE
                if top_type == "GenericError"
                else f"检测到 {top_type}，属尚未收录的错误类型，建议结合堆栈定位触发条件"
            )
        return f"【最高频：{top_type}（{count} 次）】{hint}"

    def _top_error_types(self, error_summary: dict, limit: int = 2) -> List[str]:
        """按次数降序取前 N 类错误（次数相同按类型名稳定排序）"""
        error_types = error_summary.get("error_types", {})
        ranked = sorted(error_types.items(), key=lambda kv: (-kv[1], kv[0]))
        return [type_name for type_name, _ in ranked[:limit]]

    def _generate_suggestions(self, error_summary: dict) -> List[str]:
        """生成修复建议

        v0.1.6: 只看前 2 类错误（最高频的矛盾优先），并统一重新编号——
        旧实现按类型逐段拼接，输出里会出现两组 "1. 2. 3." 的重复编号。
        """
        raw_suggestions: List[str] = []
        for type_name in self._top_error_types(error_summary, limit=2):
            raw_suggestions.extend(SUGGESTION_HINTS.get(type_name, []))

        if not raw_suggestions:
            raw_suggestions = list(GENERIC_SUGGESTIONS)

        return [f"{i}. {text}" for i, text in enumerate(raw_suggestions, 1)]
    
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
            analyzed_lines=len(entries),
            error_samples=self._collect_error_samples(error_summary)
        )
