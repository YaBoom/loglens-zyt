"""
LogLens - 智能日志分析工具
"""

__version__ = "0.1.0"
__author__ = "YaBoom"

from .analyzer import LogAnalyzer
from .parser import LogParser

__all__ = ["LogAnalyzer", "LogParser"]
