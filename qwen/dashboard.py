#!/usr/bin/env python3
"""Read-only web page for watching the Qwen loop: turn timeline, the code it is writing,
and the raw loop console, side by side. Standard library only.

  python3 qwen/dashboard.py --host <lan-address> --port 8770
"""

import argparse
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BENCH = Path.home() / "psb" / "bench"
RUNS = BENCH / "qwen" / "runs"
TEXT_LIMIT = 4000


def latest_run():
    runs = [p for p in RUNS.iterdir() if p.is_dir()] if RUNS.exists() else []
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def variant_of(run):
    return run.name.split("-", 2)[2] if run and run.name.count("-") >= 2 else None


def tmux_tail(session, lines):
    """The loop's own console, straight from its tmux pane."""
    r = subprocess.run(["tmux", "capture-pane", "-p", "-J", "-t", session, "-S", f"-{lines}"],
                       capture_output=True, text=True)
    return r.stdout.rstrip() if r.returncode == 0 else ""


def state():
    run = latest_run()
    variant = variant_of(run)
    turns = []
    if run and (run / "turns.jsonl").exists():
        for line in (run / "turns.jsonl").read_text(errors="replace").splitlines():
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    files = {}
    if variant:
        crate = BENCH / "pumpkin" / variant
        for f in sorted(crate.rglob("*")):
            if f.is_file() and "target" not in f.parts and f.stat().st_size < 400_000:
                files[str(f.relative_to(crate))] = f.read_text(errors="replace")
    console = tmux_tail("qwen", 400)
    log = BENCH / "qwen" / f"loop-{variant}.log" if variant else None
    if not console and log and log.exists():
        console = "\n".join(log.read_text(errors="replace").splitlines()[-400:])
    gates = [t for t in turns if t.get("tool") == "run_gate"]
    return {
        "run": run.name if run else None,
        "variant": variant,
        "turns": turns,
        "files": files,
        "console": console,
        "finished": any(t.get("tool") == "finish" for t in turns),
        "gates": [{"turn": g["turn"], "passed": "GATE PASSED" in (g.get("result") or "")} for g in gates],
        "status": read_json(run / "status.json") if run else None,
        "escalation": read_text(run / "ESCALATION.md") if run else None,
        "handoffs": {f.name: f.read_text(errors="replace") for f in sorted(run.glob("HANDOFF-*.md"))} if run else {},
        "hint_pending": bool(run and (run / "hint.md").exists()),
    }


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_text(path):
    return path.read_text(errors="replace") if path.exists() else None


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Qwen Loop Watch</title>
<style>
:root{--bg:#0f1115;--panel:#161a21;--line:#262c36;--text:#d7dce4;--dim:#8a94a3;--accent:#7cc4ff;
--ok:#5fd38d;--bad:#ff6b6b;--warn:#f2c14e;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:16px;margin:0}.pill{padding:2px 8px;border-radius:999px;background:var(--panel);
border:1px solid var(--line);color:var(--dim);font-size:12px}.pill.ok{color:var(--ok);border-color:var(--ok)}
.pill.bad{color:var(--bad);border-color:var(--bad)}.pill.live{color:var(--accent);border-color:var(--accent)}
main{display:grid;grid-template-columns:minmax(300px,1.1fr) minmax(320px,1.4fr) minmax(280px,1fr);
gap:1px;background:var(--line);height:calc(100% - var(--top,50px))}
#steer{border-bottom:1px solid var(--line);padding:10px 16px;display:grid;gap:8px}
#escalation{display:none;border:1px solid var(--warn);border-radius:6px;padding:10px 12px;background:rgba(242,193,78,.06)}
#escalation h3{margin:0 0 6px;font-size:14px;color:var(--warn)}
#escalation pre{max-height:180px;overflow:auto;color:var(--text)}
#hintForm{display:flex;gap:8px;align-items:flex-start}
#hintForm textarea{flex:1;min-height:38px;height:38px;resize:vertical;background:var(--panel);color:var(--text);
border:1px solid var(--line);border-radius:4px;padding:8px;font:13px var(--mono)}
#hintForm button{background:var(--panel);color:var(--text);border:1px solid var(--accent);border-radius:4px;
padding:8px 12px;cursor:pointer;font:13px system-ui}#hintForm button:disabled{opacity:.5;cursor:default}
#hintMsg{font-size:12px;color:var(--dim)}
section{background:var(--bg);display:flex;flex-direction:column;min-height:0}
section h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin:0;
padding:8px 12px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
.scroll{overflow:auto;flex:1;min-height:0}
.turn{padding:8px 12px;border-bottom:1px solid var(--line)}
.turn .head{display:flex;gap:8px;align-items:baseline}.turn .n{color:var(--dim);font:12px var(--mono);min-width:3.5em}
.turn .tool{font:600 13px var(--mono);color:var(--accent)}.turn .tool.gate{color:var(--warn)}
.turn .arg{font:12px var(--mono);color:var(--text);overflow-wrap:anywhere}.turn .secs{margin-left:auto;color:var(--dim);font-size:12px}
details{margin-top:4px}summary{cursor:pointer;color:var(--dim);font-size:12px}
pre{margin:0;font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.result{background:var(--panel);padding:6px 8px;border-radius:4px;margin-top:4px;max-height:220px;overflow:auto}
.result.ok{border-left:3px solid var(--ok)}.result.bad{border-left:3px solid var(--bad)}
.thinking{color:var(--dim);padding:4px 0}
.tabs{display:flex;gap:4px;flex-wrap:wrap;padding:6px 8px;border-bottom:1px solid var(--line)}
.tabs button{background:var(--panel);color:var(--dim);border:1px solid var(--line);border-radius:4px;
padding:3px 8px;font:12px var(--mono);cursor:pointer}.tabs button.on{color:var(--text);border-color:var(--accent)}
.code{counter-reset:ln}.code .l{display:block;padding-left:4.2em;text-indent:-4.2em}
.code .l::before{counter-increment:ln;content:counter(ln);display:inline-block;width:3.4em;margin-right:.8em;
text-align:right;color:#4a5363;text-indent:0}
.code .l.changed{background:rgba(124,196,255,.10)}
#console{padding:8px 12px}.empty{color:var(--dim);padding:16px}
label.follow{font-size:12px;color:var(--dim);text-transform:none;letter-spacing:0}
@media (max-width:900px){main{grid-template-columns:1fr;height:auto}section{height:70vh}}
</style></head><body>
<header><h1>Qwen Loop Watch</h1><span id="run" class="pill">-</span><span id="count" class="pill">-</span>
<span id="gate" class="pill">no gate yet</span><span id="state" class="pill">-</span>
<span id="live" class="pill live">live</span></header>
<div id="steer"><div id="escalation"><h3 id="escTitle"></h3><pre id="escBody"></pre></div>
<form id="hintForm"><textarea id="hint" placeholder="Hint for Qwen. Delivered at the start of its next turn, or answers an escalation. Write stop to end the run."></textarea>
<button id="send" type="submit">Send hint</button></form><div id="hintMsg"></div></div>
<main>
<section><h2>Turns <label class="follow"><input type="checkbox" id="followTurns" checked> follow</label></h2>
<div class="scroll" id="turns"></div></section>
<section><h2>Code <span id="codeinfo"></span></h2><div class="tabs" id="tabs"></div>
<div class="scroll"><pre class="code" id="code"></pre></div></section>
<section><h2>Console <label class="follow"><input type="checkbox" id="followConsole" checked> follow</label></h2>
<div class="scroll" id="consoleWrap"><pre id="console"></pre></div></section>
</main>
<script>
const $=id=>document.getElementById(id);let shownTurns=0,files={},prevFiles={},tab=null,lastRun=null;
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
function argText(t){const a=t.args||{};return a.path||a.pattern||a.summary||"";}
function turnHtml(t){const r=t.result||"",gate=t.tool==="run_gate";
const cls=r.includes("GATE PASSED")?"ok":(/error|FAILED|rejected/i.test(r)?"bad":"");
return `<div class="turn"><div class="head"><span class="n">#${t.turn}</span><span class="tool ${gate?"gate":""}">${esc(t.tool)}</span>
<span class="arg">${esc(argText(t))}</span><span class="secs">${t.secs!=null?t.secs+"s":""}</span></div>
${t.reasoning?`<details><summary>thinking</summary><pre class="thinking">${esc(t.reasoning)}</pre></details>`:""}
${r?`<details ${cls||gate?"open":""}><summary>result</summary><pre class="result ${cls}">${esc(r)}</pre></details>`:""}</div>`;}
function renderCode(){const names=Object.keys(files);if(!names.length){$("tabs").innerHTML="";$("code").innerHTML='<span class="empty">No files yet.</span>';return;}
if(!tab||!files[tab])tab=names.includes("src/lib.rs")?"src/lib.rs":names[0];
$("tabs").innerHTML=names.map(n=>`<button class="${n===tab?"on":""}" data-f="${esc(n)}">${esc(n)}</button>`).join("");
const old=(prevFiles[tab]||"").split("\n"),cur=(files[tab]||"").split("\n");
$("code").innerHTML=cur.map((l,i)=>`<span class="l ${prevFiles[tab]!==undefined&&old[i]!==l?"changed":""}">${esc(l)||" "}</span>`).join("");
$("codeinfo").textContent=`${cur.length} lines`;}
$("tabs").addEventListener("click",e=>{const f=e.target.dataset.f;if(f){tab=f;renderCode();}});
async function poll(){try{const s=await (await fetch("api/state",{cache:"no-store"})).json();
if(s.run!==lastRun){shownTurns=0;$("turns").innerHTML="";lastRun=s.run;}
$("run").textContent=s.run||"no run";const lastTurn=s.turns.length?s.turns.at(-1).turn:0;$("count").textContent=`turn ${lastTurn} · ${s.turns.length} tool calls`;
const g=s.gates.at(-1);$("gate").className="pill "+(g?(g.passed?"ok":"bad"):"");
$("gate").textContent=s.finished?"finished":(g?`gate ${g.passed?"passed":"failed"} (turn ${g.turn}, ${s.gates.length} runs)`:"no gate yet");
if(s.turns.length>shownTurns){$("turns").insertAdjacentHTML("beforeend",s.turns.slice(shownTurns).map(turnHtml).join(""));
shownTurns=s.turns.length;if($("followTurns").checked)$("turns").scrollTop=1e9;}
if(JSON.stringify(s.files)!==JSON.stringify(files)){prevFiles=files;files=s.files;renderCode();}
if($("console").textContent!==s.console){$("console").textContent=s.console;if($("followConsole").checked)$("consoleWrap").scrollTop=1e9;}
const st=s.status||{};$("state").textContent=st.state||"-";
$("state").className="pill "+(st.state==="waiting-for-hint"?"bad":st.state==="finished"?"ok":"");
if(s.escalation){$("escalation").style.display="block";const lines=s.escalation.split("\n");
$("escTitle").textContent=lines[0].replace(/^#\s*/,"");$("escBody").textContent=lines.slice(1).join("\n").trim();}
else $("escalation").style.display="none";
$("send").disabled=s.hint_pending;if(s.hint_pending)$("hintMsg").textContent="Hint waiting to be picked up by the loop.";
document.documentElement.style.setProperty("--top",(document.querySelector("header").offsetHeight+$("steer").offsetHeight)+"px");
$("live").textContent="live";$("live").className="pill live";}catch(e){$("live").textContent="disconnected";$("live").className="pill bad";}}
$("hintForm").addEventListener("submit",async e=>{e.preventDefault();const text=$("hint").value.trim();if(!text)return;
$("send").disabled=true;try{const r=await fetch("api/hint",{method:"POST",headers:{"Content-Type":"text/plain"},body:text});
$("hintMsg").textContent=r.ok?"Sent. Qwen gets it at the start of its next turn.":"Not sent: "+(await r.text());
if(r.ok)$("hint").value="";}catch(err){$("hintMsg").textContent="Not sent: "+err;}poll();});
poll();setInterval(poll,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            body, ctype = json.dumps(state()).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/hint":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        text = self.rfile.read(min(length, 20_000)).decode("utf-8", "replace").strip()
        run = latest_run()
        if not text or run is None:
            self.send_error(400, "empty hint or no run")
            return
        if (run / "hint.md").exists():
            self.send_error(409, "a hint is already waiting")
            return
        tmp = run / "hint.md.tmp"
        tmp.write_text(text + "\n")
        tmp.rename(run / "hint.md")
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    a = ap.parse_args()
    print(f"Qwen loop watch on http://{a.host}:{a.port}/", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
