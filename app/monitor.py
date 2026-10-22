"""Task monitor UI served separately from the assistant API."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nest tasks</title>
<style>
:root{color-scheme:dark;--bg:#111318;--panel:#191c23;--line:#2a2f3a;--muted:#8f98a8;--text:#e7ebf2;--ok:#55d187;--warn:#f2c14e;--bad:#ff6b6b;--accent:#7aa2ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:rgba(17,19,24,.95);border-bottom:1px solid var(--line);padding:14px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0;letter-spacing:.02em}main{padding:16px 20px 48px;max-width:1400px;margin:auto}
.bar{display:flex;gap:10px;align-items:center;color:var(--muted);font-size:12px;flex-wrap:wrap}
.pill{border:1px solid var(--line);border-radius:999px;padding:4px 9px;background:var(--panel)}button{background:#222632;color:var(--text);border:1px solid #363c4a;border-radius:7px;padding:6px 10px;cursor:pointer}button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:not-allowed}
.grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr);gap:18px}@media(max-width:1050px){.grid{grid-template-columns:1fr}}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 8px}
.task{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:0 0 12px;overflow:hidden}
.task-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;padding:11px 12px;border-bottom:1px solid var(--line)}
.task-id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
.task-meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:5px;color:var(--muted);font-size:12px}
.badge{border-radius:999px;padding:2px 8px;font-size:11px;font-weight:650;text-transform:uppercase;letter-spacing:.04em;border:1px solid transparent;white-space:nowrap}
.loading{color:#111;background:var(--warn)}.done{color:#08120c;background:var(--ok)}.error{color:#fff;background:var(--bad)}.skipped{color:#111;background:#b9c1d0}.interrupted{color:#111;background:#d8a4ff}
.stages{width:100%;border-collapse:collapse}.stages td,.stages th{padding:7px 10px;text-align:left;border-bottom:1px solid #232833;vertical-align:top}.stages th{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}.stages tr:last-child td{border-bottom:0}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.detail{color:var(--muted);word-break:break-word;white-space:pre-wrap;max-height:130px;overflow:auto}
.err{margin:0 12px 12px;border:1px solid #65353c;background:#2a171b;border-radius:8px;padding:10px;color:#ffc7cc;white-space:pre-wrap;word-break:break-word}
.requests{background:var(--panel);border:1px solid var(--line);border-radius:10px;max-height:calc(100vh - 150px);overflow:auto}
.req{padding:8px 10px;border-bottom:1px solid #232833;font-size:12px}.req:last-child{border-bottom:0}.req-top{display:flex;gap:8px;justify-content:space-between;color:var(--muted)}.req-path{word-break:break-all;margin-top:2px}.empty{padding:18px;color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>Nest tasks</h1>
  <div class="bar">
    <span class="pill" id="summary">waiting</span>
    <span id="updated">-</span>
    <button id="pause">Pause</button>
    <button id="refresh">Refresh</button>
  </div>
</header>
<main>
  <div class="grid">
    <section><h2>Pipeline</h2><div id="tasks" class="empty">Loading tasks...</div></section>
    <section><h2>Recent requests</h2><div id="requests" class="requests"><div class="empty">Loading requests...</div></div></section>
  </div>
</main>
<script>
const state={paused:false};
const $=id=>document.getElementById(id);
function esc(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function fmtTime(ts){if(!ts)return "-";const d=new Date(ts*1000);return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});}
function fmtElapsed(a,b){if(!a)return "";const end=b||Date.now()/1000;return ((end-a).toFixed(end-a<10?2:1))+"s";}
function badge(status){const s=status||"loading";return `<span class="badge ${esc(s)}">${esc(s)}</span>`;}
function jsonDetail(detail){if(!detail||!Object.keys(detail).length)return "";return `<div class="detail">${esc(JSON.stringify(detail,null,2))}</div>`;}
function renderTasks(tasks){
  if(!tasks.length){$("tasks").className="empty";$("tasks").textContent="No tasks yet.";return;}
  $("tasks").className="";
  $("tasks").innerHTML=tasks.map(task=>{
    const stages=(task.stages||[]).map(stage=>`<tr>
      <td class="mono">${esc(stage.name)}</td>
      <td>${badge(stage.status)}</td>
      <td class="mono">${esc(fmtTime(stage.started))}</td>
      <td class="mono">${esc(fmtElapsed(stage.started,stage.ended))}</td>
      <td>${jsonDetail(stage.detail)}</td></tr>`).join("");
    const error=task.error?`<div class="err">${esc(task.error.stage)}: ${esc(task.error.message)}</div>`:"";
    const button=task.status==="loading"?`<button data-interrupt="${esc(task.id)}">Interrupt</button>`:"";
    const meta=task.meta&&Object.keys(task.meta).length?`<div class="task-meta">${Object.entries(task.meta).map(([k,v])=>`<span>${esc(k)}=${esc(typeof v==="object"?JSON.stringify(v):v)}</span>`).join("")}</div>`:"";
    return `<article class="task"><div class="task-head"><div><div class="task-id">${esc(task.id)}</div>${meta}</div><div class="bar">${badge(task.status)}${button}</div></div>
      <table class="stages"><thead><tr><th>stage</th><th>status</th><th>start</th><th>elapsed</th><th>detail</th></tr></thead><tbody>${stages}</tbody></table>${error}</article>`;
  }).join("");
  document.querySelectorAll("[data-interrupt]").forEach(button=>button.addEventListener("click",()=>interrupt(button.dataset.interrupt)));
}
function renderRequests(requests){
  if(!requests.length){$("requests").innerHTML='<div class="empty">No requests yet.</div>';return;}
  $("requests").innerHTML=requests.map(req=>{
    const bits=[req.kind,req.method,req.host,req.path,req.status,req.bytes!==undefined?req.bytes+" B":"",req.flow||""].filter(Boolean).map(esc).join(" ");
    return `<div class="req"><div class="req-top"><span>${esc(req.kind||"event")}</span><span>${esc(fmtTime(Date.parse(req.time)/1000))}</span></div><div class="req-path">${bits}</div></div>`;
  }).join("");
}
async function refresh(){
  if(state.paused)return;
  try{
    const [tasks,requests]=await Promise.all([fetch("/api/tasks").then(r=>r.json()),fetch("/api/requests").then(r=>r.json())]);
    renderTasks(tasks.tasks||[]);renderRequests(requests.requests||[]);
    const active=(tasks.tasks||[]).filter(t=>t.status==="loading").length;
    $("summary").textContent=`${(tasks.tasks||[]).length} tasks - ${active} loading`;
    $("updated").textContent="updated "+new Date().toLocaleTimeString();
  }catch(err){$("summary").textContent="monitor error: "+err;}
}
async function interrupt(id){await fetch("/api/tasks/"+encodeURIComponent(id)+"/interrupt",{method:"POST"});await refresh();}
$("pause").addEventListener("click",()=>{state.paused=!state.paused;$("pause").textContent=state.paused?"Resume":"Pause";if(!state.paused)refresh();});
$("refresh").addEventListener("click",refresh);
setInterval(refresh,1000);refresh();
</script>
</body>
</html>"""


def _recent_requests(log_path: Path, limit: int = 50) -> list[dict]:
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    wanted = {
        "request", "stream_pending", "stream_capture", "stream_segments",
        "tts_served", "local_answer", "local_assistant_error", "local_wake_only",
        "s3_local_route", "s3_request", "s3_segments", "s3_response", "s3_query_error",
        "s3_send_error", "s3_reset", "s3_connection_error",
        "s3_frame", "s3_heartbeat_error", "s3_upload_complete", "s3_no_upstream_audio",
        "s3_endpoint", "s3_endpoint_error", "s3_quiet_close", "local_follow_on_noise", "s3_recognition_init", "s3_control_reply",
        "s3_tls_error", "s3_tls_established",
    }
    out = []
    for line in reversed(lines[-500:]):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("kind") in wanted:
            out.append(item)
        if len(out) >= limit:
            break
    return list(reversed(out))


class MonitorHandler(BaseHTTPRequestHandler):
    store = None
    log_path = Path("data/logs/proxy-events.jsonl")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/tasks":
            self._json(200, {"tasks": self.store.snapshot()})
            return
        if path == "/api/requests":
            self._json(200, {"requests": _recent_requests(self.log_path)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        prefix = "/api/tasks/"
        suffix = "/interrupt"
        if path.startswith(prefix) and path.endswith(suffix):
            task_id = unquote(path[len(prefix):-len(suffix)])
            ok = self.store.cancel(task_id)
            self._json(200 if ok else 404, {"interrupted": ok, "id": task_id})
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args) -> None:
        return


def start_monitor(store, *, host: str, port: int, log_path: Path) -> ThreadingHTTPServer:
    handler = type("BoundMonitorHandler", (MonitorHandler,), {"store": store, "log_path": log_path})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="task-monitor", daemon=True)
    thread.start()
    return server
