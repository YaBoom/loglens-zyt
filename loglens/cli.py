"""
LogLens CLI - 命令行界面
"""

import sys
import time
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from .analyzer import LogAnalyzer

console = Console()


@click.group()
@click.version_option(version="0.1.0")
def main():
    """LogLens - 智能日志分析工具 🔍"""
    pass


@main.command()
@click.argument("log_file", type=click.Path(exists=True))
@click.option("--tail", "-t", type=int, help="只分析最后N行")
@click.option("--level", "-l", default="ERROR", help="最低日志级别 (DEBUG|INFO|WARN|ERROR)")
@click.option("--output", "-o", type=click.Path(), help="输出报告到文件")
def analyze(log_file, tail, level, output):
    """分析日志文件"""
    
    console.print(f"\n🔍 正在分析日志: {log_file}")
    if tail:
        console.print(f"📄 只分析最后 {tail} 行")
    console.print(f"🎯 最低级别: {level}\n")
    
    start_time = time.time()
    
    try:
        analyzer = LogAnalyzer()
        report = analyzer.analyze(log_file, tail=tail, min_level=level)
        
        elapsed = time.time() - start_time
        
        # 显示报告
        _display_report(report, elapsed)
        
        # 保存到文件
        if output:
            _save_report(report, output)
        
    except Exception as e:
        console.print(f"\n❌ 分析失败: {str(e)}", style="red")
        sys.exit(1)


@main.command()
@click.option("--level", "-l", default="ERROR", help="最低日志级别")
def shell(level):
    """交互式分析模式"""
    
    console.print(Panel.fit(
        "[bold cyan]LogLens 交互式分析模式[/bold cyan]\n"
        "输入日志内容进行分析，输入 'quit' 或 'exit' 退出",
        border_style="cyan"
    ))
    
    analyzer = LogAnalyzer()
    
    while True:
        try:
            console.print("\n📋 请粘贴日志内容 (输入空行开始分析):")
            lines = []
            while True:
                line = input()
                if not line:
                    break
                lines.append(line)
            
            content = "\n".join(lines)
            if not content.strip():
                continue
            
            if content.lower() in ["quit", "exit"]:
                console.print("👋 再见!")
                break
            
            report = analyzer.analyze_stdin(content, min_level=level)
            _display_report(report, 0)
            
        except KeyboardInterrupt:
            console.print("\n👋 再见!")
            break
        except Exception as e:
            console.print(f"❌ 分析失败: {str(e)}", style="red")


@main.command()
@click.argument("log_file", type=click.Path(exists=True))
@click.option("--interval", "-i", default=5, help="监控间隔(秒)")
def monitor(log_file, interval):
    """实时监控日志文件"""
    
    console.print(Panel.fit(
        f"[bold yellow]实时监控模式[/bold yellow]\n"
        f"文件: {log_file}\n"
        f"间隔: {interval}秒\n"
        "按 Ctrl+C 退出",
        border_style="yellow"
    ))
    
    analyzer = LogAnalyzer()
    last_size = 0
    
    try:
        while True:
            current_size = os.path.getsize(log_file)
            
            if current_size > last_size:
                # 读取新增内容
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(last_size)
                    new_content = f.read()
                
                if new_content.strip():
                    console.print("\n" + "="*50)
                    console.print(f"🆕 新增日志 ({current_size - last_size} bytes)")
                    
                    report = analyzer.analyze_stdin(new_content)
                    _display_report(report, 0)
                
                last_size = current_size
            
            time.sleep(interval)
            
    except KeyboardInterrupt:
        console.print("\n👋 监控结束")


def _display_report(report, elapsed):
    """显示分析报告"""
    
    # 摘要
    console.print(Panel(
        report.summary,
        title="📊 分析摘要",
        border_style="green"
    ))
    
    # 根因
    console.print(Panel(
        report.root_cause,
        title="🎯 根因分析",
        border_style="red"
    ))
    
    # 建议
    suggestions_text = "\n".join(report.suggestions)
    console.print(Panel(
        suggestions_text,
        title="💡 修复建议",
        border_style="yellow"
    ))
    
    # 统计
    table = Table(title="📈 统计信息", show_header=False)
    table.add_column("指标", style="cyan")
    table.add_column("值", style="green")
    
    table.add_row("分析行数", str(report.analyzed_lines))
    table.add_row("错误数量", str(report.error_count))
    table.add_row("警告数量", str(report.warning_count))
    
    if elapsed > 0:
        table.add_row("耗时", f"{elapsed:.2f}秒")
    
    console.print(table)


def _save_report(report, output_path):
    """保存报告到文件"""
    content = f"""# LogLens 分析报告

## 摘要
{report.summary}

## 根因分析
{report.root_cause}

## 修复建议
{chr(10).join(report.suggestions)}

## 统计
- 分析行数: {report.analyzed_lines}
- 错误数量: {report.error_count}
- 警告数量: {report.warning_count}
"""
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    console.print(f"\n✅ 报告已保存到: {output_path}")


# 添加 os 导入
import os


if __name__ == "__main__":
    main()
