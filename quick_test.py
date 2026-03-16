"""简单测试 LLM 调用"""
import requests
import json

API_KEY = "sk-a5f788a3a1af4ff79f5f8f18f7c62df0"
URL = "https://api.deepseek.com/v1/chat/completions"

print("测试 DeepSeek API...")

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

data = {
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "回复 OK"}],
    "max_tokens": 10
}

try:
    resp = requests.post(URL, headers=headers, json=data, timeout=30)
    print(f"状态码: {resp.status_code}")
    print(f"响应: {resp.text}")
except Exception as e:
    print(f"错误: {e}")
