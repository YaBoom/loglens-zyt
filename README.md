# LogLens 🔍

> 智能日志分析工具 - 用AI快速定位线上问题根因

[![Python Version](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

LogLens 是一个基于大模型的智能日志分析工具，帮助开发者快速定位线上问题的根因，并给出修复建议。

## ✨ 特性

- 🧠 **智能分析** - AI理解日志上下文，定位真正的问题根因
- ⚡ **快速定位** - 几秒钟内从海量日志中提取关键信息
- 📊 **可视化报告** - 生成结构化的分析报告
- 🔧 **修复建议** - 提供具体的代码修复方案
- 🖥️ **CLI工具** - 轻量级命令行工具，随装随用
- 🌐 **Web界面** - 支持拖拽上传，在线分析

## 📦 安装

```bash
pip install loglens
```

或者从源码安装：

```bash
git clone https://github.com/YaBoom/loglens-zyt.git
cd loglens-zyt
pip install -e .
```

### 依赖说明

LogLens 使用 `requests` 库调用 DeepSeek LLM API，请确保已安装：

```bash
pip install requests
```

## 🔑 API Key 配置

LogLens 支持 DeepSeek 大模型进行智能根因分析，需要配置 API Key：

```bash
# 方式1: 通过命令行参数
loglens analyze app.log -k sk-your-api-key

# 方式2: 通过环境变量（推荐）
set DEEPSEEK_API_KEY=sk-your-api-key
loglens analyze app.log
```

获取 API Key: [DeepSeek开放平台](https://platform.deepseek.com/)

## 🚀 快速开始

### 命令行使用

```bash
# 分析日志文件（需要配置API Key才能使用LLM分析）
loglens analyze /var/log/app.log -k sk-your-api-key

# 分析最近100行
loglens analyze /var/log/app.log --tail 100

# 指定错误级别（默认ERROR）
loglens analyze /var/log/app.log --level error

# 也支持WARN级别
loglens analyze /var/log/app.log --level warn

# 交互式分析
loglens shell -k sk-your-api-key
```

### Python API

```python
from loglens import LogAnalyzer

# 方式1: 通过参数传入API Key
analyzer = LogAnalyzer(api_key="sk-your-api-key")

# 方式2: 通过环境变量（自动读取DEEPSEEK_API_KEY）
analyzer = LogAnalyzer()

report = analyzer.analyze("path/to/logfile.log")
print(report.summary)      # 分析摘要
print(report.root_cause)   # 根因分析
print(report.suggestions)  # 修复建议
```

### Web界面

```bash
loglens web
# 打开 http://localhost:8080
```

## 📖 使用示例

### 1. 分析应用日志（LLM智能分析）

```bash
$ loglens analyze app.log -k sk-your-api-key

🔍 正在分析日志: app.log
🎯 最低级别: ERROR

[DEBUG] API Key已设置: True
[DEBUG] LLM Client已初始化: True
[DEBUG] 正在调用 DeepSeek LLM...
[DEBUG] LLM 调用成功!

┌─────────────────────────────────────────┐
│ 📊 分析摘要                              │
├─────────────────────────────────────────┤
│ 检测到 2 个错误:                         │
│   - GenericError: 2 个                   │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ 🎯 根因分析                              │
├─────────────────────────────────────────┤
│ UserService.getUser方法调用时出现        │
│ GenericError，可能是数据库连接异常、     │
│ 用户数据不存在或参数验证失败导致         │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ 💡 修复建议                              │
├─────────────────────────────────────────┤
│ 1. 检查数据库连接状态和用户数据是否存在   │
│ 2. 在UserService.getUser方法中添加       │
│    更详细的错误日志                      │
│ 3. 实现重试机制或降级策略                │
└─────────────────────────────────────────┘

╔════════════════════════════════════════╗
│ 📈 统计信息                             │
├──────────────┬─────────────────────────┤
│ 分析行数     │ 4                       │
│ 错误数量     │ 2                       │
│ 警告数量     │ 0                       │
│ 耗时         │ 4.61秒                  │
╚════════════════════════════════════════╝
```

### 2. 实时监控模式

```bash
loglens monitor /var/log/app.log --watch
```

## 🏗️ 项目结构

```
loglens/
├── loglens/
│   ├── __init__.py
│   ├── analyzer.py      # 日志分析核心（含LLM集成）
│   ├── parser.py        # 日志解析器
│   ├── cli.py           # 命令行界面
│   ├── llm_client.py    # DeepSeek LLM 客户端
│   └── utils.py         # 工具函数
├── tests/               # 测试用例
├── docs/               # 文档
├── pyproject.toml
└── README.md
```

## 🔧 高级配置

### 使用其他模型

```python
from loglens.llm_client import LLMClient

# 使用自定义模型
client = LLMClient(api_key="sk-your-key", model="deepseek-coder")
```

### 调试模式

运行时会输出调试信息：
- `[DEBUG] API Key已设置: True/False`
- `[DEBUG] LLM Client已初始化: True/False`
- `[DEBUG] 错误消息数量: X`
- `[DEBUG] 正在调用 DeepSeek LLM...`

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License

---

<p align="center">Made with ❤️ by YaBoom</p>
