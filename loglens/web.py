"""
LogLens Web - 本地 Web 界面 (v0.1.8)

兑现 README 的承诺: `loglens web` 启动一个只在 127.0.0.1 监听的本地服务，
浏览器里粘贴 / 拖拽日志即可分析。

隐私边界（背调结论: 同类工具最突出的共性卖点是 "100% 本地处理"）:
- 服务只绑定本机回环地址，日志文本仅发送到你自己电脑上的这个进程；
- 分析默认走本地规则引擎；只有配置了 DEEPSEEK_API_KEY 时才会把错误样本
  发给你自己配置的 LLM 服务，且行为与 CLI 完全一致（失败自动回退本地）。

零第三方依赖: 只用标准库 http.server / json / html。拖拽上传由页面里的
十几行原生 JS (FileReader) 完成，不需要任何前端构建链。
"""

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .analyzer import LogAnalyzer, AnalysisReport
from . import __version__

# 单次分析的内容上限: 10MB。浏览器粘贴几十 MB 文本会把页面拖死，
# 而且本地分析对超大日志本来就该先 tail/head 截取。
MAX_CONTENT_BYTES = 10 * 1024 * 1024

VALID_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def _report_to_dict(report: AnalysisReport, elapsed: float) -> dict:
    """AnalysisReport → JSON。前端是瘦客户端，只负责渲染。"""
    return {
        "ok": True,
        "summary": report.summary,
        "root_cause": report.root_cause,
        "suggestions": report.suggestions,
        "error_samples": report.error_samples,
        "stats": {
            "analyzed_lines": report.analyzed_lines,
            "event_count": report.event_count,
            "error_count": report.error_count,
            "warning_count": report.warning_count,
            "elapsed": round(elapsed, 2),
        },
    }


class _WebHandler(BaseHTTPRequestHandler):
    server_version = "LogLens/" + __version__

    # 静默访问日志: 本地工具每次请求都往控制台刷一行没有价值，
    # 真正需要看的错误在响应里已经体现了。
    def log_message(self, format, *args):  # noqa: A002 - 标准库签名
        pass

    # ---------- GET ----------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._respond(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"))
        else:
            self._respond(404, "application/json; charset=utf-8",
                          b'{"ok": false, "error": "not found"}')

    # ---------- POST ----------

    def do_POST(self):
        if self.path != "/analyze":
            self._respond(404, "application/json; charset=utf-8",
                          b'{"ok": false, "error": "not found"}')
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_CONTENT_BYTES:
            self._respond(400, "application/json; charset=utf-8",
                          b'{"ok": false, "error": "content too large or empty (limit 10MB)"}')
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            content = payload.get("content", "")
            level = str(payload.get("level", "ERROR")).upper()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(400, "application/json; charset=utf-8",
                          b'{"ok": false, "error": "invalid JSON body"}')
            return

        if level not in VALID_LEVELS:
            level = "ERROR"

        import time
        start = time.time()
        try:
            # 每次请求一个新 analyzer: 无状态、线程安全，也避免复用 LLM 客户端
            # 在并发下共享半初始化状态。
            analyzer = LogAnalyzer()
            report = analyzer.analyze_stdin(content, min_level=level)
        except Exception as e:  # 分析器任何异常都不能带崩服务
            self._respond(500, "application/json; charset=utf-8",
                          json.dumps({"ok": False, "error": f"analysis failed: {e}"},
                                     ensure_ascii=False).encode("utf-8"))
            return

        body = json.dumps(_report_to_dict(report, time.time() - start),
                          ensure_ascii=False).encode("utf-8")
        self._respond(200, "application/json; charset=utf-8", body)

    # ---------- helpers ----------

    def _respond(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 只在回环地址上服务，但显式禁止缓存/跨站，养成干净习惯
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LogLens - 本地日志分析</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --txt:#e2e8f0; --dim:#94a3b8; --accent:#38bdf8; --ok:#4ade80; --warn:#fbbf24; --err:#f87171; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--txt); font:15px/1.6 "Segoe UI", "Microsoft YaHei", sans-serif; }
  .wrap { max-width: 900px; margin: 0 auto; padding: 24px 16px 60px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 20px; }
  #drop { border: 2px dashed var(--line); border-radius: 12px; padding: 26px; text-align: center;
          color: var(--dim); transition: border-color .2s, background .2s; cursor: pointer; }
  #drop.on { border-color: var(--accent); background: #16233b; }
  textarea { width: 100%; min-height: 180px; margin-top: 12px; background: var(--card); color: var(--txt);
             border: 1px solid var(--line); border-radius: 10px; padding: 12px; font: 13px/1.5 Consolas, monospace; resize: vertical; }
  .bar { display: flex; gap: 10px; align-items: center; margin: 12px 0; flex-wrap: wrap; }
  select { background: var(--card); color: var(--txt); border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; }
  button { background: var(--accent); color: #06263a; font-weight: 600; border: 0; border-radius: 8px;
           padding: 9px 22px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: wait; }
  .privacy { color: var(--dim); font-size: 12px; margin: 6px 0 18px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin: 12px 0; }
  .card h2 { font-size: 14px; margin: 0 0 8px; color: var(--accent); }
  .card.error h2 { color: var(--err); } .card.ok h2 { color: var(--ok); } .card.warn h2 { color: var(--warn); }
  pre { white-space: pre-wrap; word-break: break-all; margin: 0; font: 13px/1.6 Consolas, monospace; }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; }
  .chip { background: #0b1220; border: 1px solid var(--line); border-radius: 999px; padding: 3px 12px; font-size: 12.5px; }
  #busy { color: var(--dim); display: none; }
  .hidden { display: none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>🔍 LogLens 本地日志分析</h1>
  <div class="sub">粘贴或拖入日志 → 立刻得到错误分组、根因推断与修复建议</div>

  <div id="drop">📥 把 .log / .txt 文件拖到这里（点击也可选择文件）</div>
  <input id="file" type="file" class="hidden">
  <textarea id="log" placeholder="或直接粘贴日志内容，例如：&#10;2026-03-16 10:23:46 ERROR java.lang.NullPointerException: user is null&#10;    at com.demo.UserService.getUser(UserService.java:42)"></textarea>

  <div class="bar">
    <label>最低级别
      <select id="level">
        <option>ERROR</option><option>WARN</option><option>INFO</option><option>DEBUG</option>
      </select>
    </label>
    <button id="go" onclick="run()">开始分析</button>
    <span id="busy">分析中…</span>
  </div>
  <div class="privacy">🔒 服务只运行在本机 (127.0.0.1)，日志不会上传到任何第三方；如配置了 DEEPSEEK_API_KEY，错误样本会按你的配置送 LLM 分析。</div>

  <div id="out"></div>
</div>

<script>
const drop = document.getElementById('drop'), file = document.getElementById('file'), log = document.getElementById('log');
drop.onclick = () => file.click();
file.onchange = () => file.files[0] && file.files[0].text().then(t => log.value = t);
['dragover','dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault();
  drop.classList.toggle('on', ev === 'dragover');
  if (ev === 'drop' && e.dataTransfer.files[0]) e.dataTransfer.files[0].text().then(t => log.value = t);
}));

async function run() {
  const btn = document.getElementById('go'), busy = document.getElementById('busy'), out = document.getElementById('out');
  btn.disabled = true; busy.style.display = 'inline'; out.innerHTML = '';
  try {
    const r = await fetch('/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: log.value, level: document.getElementById('level').value})
    });
    const d = await r.json();
    if (!d.ok) { out.innerHTML = card('error', '❌ 分析失败', d.error || ('HTTP ' + r.status)); return; }
    render(d);
  } catch (e) {
    out.innerHTML = card('error', '❌ 请求失败', String(e));
  } finally {
    btn.disabled = false; busy.style.display = 'none';
  }
}

function card(cls, title, body) {
  return `<div class="card ${cls}"><h2>${title}</h2><pre>${esc(body)}</pre></div>`;
}
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

function render(d) {
  const s = d.stats;
  let html = `<div class="card"><h2>📈 统计</h2><div class="chips">
    <span class="chip">分析行数 ${s.analyzed_lines}</span>
    <span class="chip">事件 ${s.event_count}</span>
    <span class="chip" style="color:${s.error_count ? 'var(--err)' : 'var(--ok)'}">错误 ${s.error_count}</span>
    <span class="chip" style="color:${s.warning_count ? 'var(--warn)' : 'var(--dim)'}">警告 ${s.warning_count}</span>
    <span class="chip">耗时 ${s.elapsed}s</span></div></div>`;
  html += card('ok', '📊 分析摘要', d.summary);
  html += card('error', '🎯 根因分析', d.root_cause);
  html += card('warn', '💡 修复建议', (d.suggestions || []).join('\n'));
  if ((d.error_samples || []).length)
    html += card('', `🔍 错误样本（${d.error_samples.length} 条去重）`, d.error_samples.join('\n'));
  document.getElementById('out').innerHTML = html;
}
</script>
</body>
</html>
"""


def create_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """创建 Web 服务（ThreadingHTTPServer: 线程级并发，LLM 调用不阻塞页面）。"""
    return ThreadingHTTPServer((host, port), _WebHandler)


def run_web(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True) -> None:
    """启动 Web 服务并尝试打开浏览器。Ctrl+C 退出。"""
    server = create_server(host, port)
    actual_port = server.server_address[1]
    url = f"http://localhost:{actual_port}"
    print(f"\n🌐 LogLens Web 已启动: {url}")
    print(f"   仅监听 {host}（本机可访问），Ctrl+C 停止\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass  # 无图形环境/打开失败都不影响服务本身
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Web 服务已停止")
    finally:
        server.server_close()
