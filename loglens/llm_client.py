"""
LLM 客户端 - DeepSeek API 调用封装
"""

import json
import requests
from typing import Optional, List


class LLMClient:
    """DeepSeek LLM 客户端"""

    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        """
        初始化 LLM 客户端

        Args:
            api_key: DeepSeek API Key
            model: 模型名称，默认 deepseek-chat
        """
        self.api_key = api_key
        self.model = model
        self.api_url = "https://api.deepseek.com/v1/chat/completions"

    def chat(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 2000
    ) -> str:
        """
        发送聊天请求

        Args:
            messages: 消息列表，格式为 [{"role": "user/system", "content": "..."}]
            temperature: 温度参数，控制随机性
            max_tokens: 最大生成token数

        Returns:
            模型回复内容
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        data = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        try:
            response = requests.post(
                self.api_url,
                headers=headers,
                json=data,
                timeout=30
            )
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except requests.exceptions.HTTPError as e:
            raise Exception(f"API请求失败: {e.response.status_code} - {e.response.text}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"网络请求失败: {str(e)}")
        except Exception as e:
            raise Exception(f"调用LLM失败: {str(e)}")

    def analyze_log_error(
        self,
        error_messages: List[str],
        error_types: dict
    ) -> dict:
        """
        分析日志错误

        Args:
            error_messages: 错误信息列表
            error_types: 错误类型统计

        Returns:
            包含 root_cause 和 suggestions 的字典
        """
        error_info = "\n".join([f"- {msg}" for msg in error_messages])
        error_type_info = "\n".join([f"- {k}: {v}个" for k, v in error_types.items()])

        system_prompt = """你是一个专业的日志分析专家，擅长从日志中找出问题的根因并给出修复建议。
请根据提供的错误信息进行分析，返回JSON格式的分析结果。
返回格式要求：
{
    "root_cause": "根因分析（简洁明了，不超过100字）",
    "suggestions": ["建议1", "建议2", "建议3"]
}
建议要具体、可操作，切中问题要害。"""

        user_prompt = f"""请分析以下日志错误：

错误信息：
{error_info}

错误类型统计：
{error_type_info}

请给出：
1. 最可能的根因是什么？
2. 需要如何修复？
3. 需要注意什么？"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        response = self.chat(messages, temperature=0.3, max_tokens=1000)

        # 解析JSON响应
        try:
            # 尝试提取JSON部分
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0]
            else:
                json_str = response

            result = json.loads(json_str.strip())
            return {
                "root_cause": result.get("root_cause", "分析失败"),
                "suggestions": result.get("suggestions", ["请查看具体错误信息"])
            }
        except json.JSONDecodeError:
            # 解析失败，返回默认格式
            return self._fallback_parse(response)

    def _fallback_parse(self, response: str) -> dict:
        """备用解析方法"""
        lines = response.strip().split("\n")
        root_cause = ""
        suggestions = []

        for line in lines:
            if "根因" in line or "原因" in line:
                root_cause = line.split("：")[-1].split(".")[-1].strip()
            elif line.strip().startswith(tuple("1234567890")):
                suggestions.append(line.strip())

        if not root_cause:
            root_cause = "从日志中检测到异常，建议查看具体错误堆栈信息"
        if not suggestions:
            suggestions = [
                "1. 查看完整错误堆栈定位具体问题",
                "2. 搜索相关错误信息",
                "3. 检查最近代码改动"
            ]

        return {
            "root_cause": root_cause,
            "suggestions": suggestions
        }


# 默认客户端实例
_default_client: Optional[LLMClient] = None


def get_llm_client(api_key: str) -> LLMClient:
    """
    获取默认的 LLM 客户端（单例模式）

    Args:
        api_key: DeepSeek API Key

    Returns:
        LLMClient 实例
    """
    global _default_client
    if _default_client is None:
        _default_client = LLMClient(api_key)
    return _default_client
