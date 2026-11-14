"""Local control and observation app for Mini_AURION runs.

One process per operator, loopback only, standard library only. It
reads the eval state directories and nothing else; its only writes
are kill receipts, caps edits, and approval decisions. It runs apart
from the orchestrator so the kill switch works while the orchestrator
is wedged inside a model call.

Usage:
    python -m mini_aurion.monitor --root eval/tiers-smoke --port 8787
then open http://127.0.0.1:8787/ in a browser.
"""
from __future__ import annotations

import html
import json
import os
import signal
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .ledger import Ledger, LedgerError
from .receipts import ReceiptStore

STALE_SECONDS = 300
KILL_WAIT_SECONDS = 5


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Windows raises PermissionError for a live foreign pid; the
        # monitor only ever kills its own operator's processes, but a
        # pid we cannot signal is not one we can kill either.
        return False
    return True


def find_tasks(root: Path) -> list[dict]:
    root = Path(root)
    """Every proc.json under root, newest first. Missing or corrupt
    entries are skipped: the monitor reports on state, never repairs."""
    tasks = []
    for proc_path in sorted(root.rglob("proc.json")):
        try:
            rel = proc_path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > 4:
            continue
        try:
            proc = json.loads(proc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        proc["_dir"] = str(proc_path.parent)
        tasks.append(proc)
    tasks.sort(key=lambda p: p.get("started", ""), reverse=True)
    return tasks


def task_status(proc: dict) -> str:
    status = proc.get("status", "")
    if status != "running":
        return status or "unknown"
    pid = proc.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return "died"
    return "running"


def last_receipt(receipts_path: str) -> dict | None:
    path = Path(receipts_path)
    if not path.exists():
        return None
    store = ReceiptStore(path)
    try:
        anchor_raw = Path(str(path) + ".anchor").read_text(
            encoding="utf-8")
        anchor_max = json.loads(anchor_raw).get("max_seq", 0)
    except (OSError, ValueError):
        anchor_max = 0
    if anchor_max and store.index_count() == anchor_max:
        tail = store.index_tail(limit=1)
        if tail:
            return tail[0]
    # Index missing or stale: read the JSONL tail directly. The
    # monitor never rebuilds the index itself — that write belongs
    # to the agent side, and the monitor's writes are kill, caps,
    # and approvals only.
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
    except OSError:
        return None
    for line in reversed(lines[-5:]):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        return {"seq": row.get("seq"), "action": row.get("action"),
                "agent_id": row.get("agent_id"), "tool": row.get("tool"),
                "outcome": row.get("outcome"), "ts": row.get("ts")}
    return None


def receipt_age_seconds(row: dict | None) -> float | None:
    if not row or not row.get("ts"):
        return None
    try:
        stamp = time.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return time.time() - time.mktime(stamp)


def task_spend(proc: dict) -> dict:
    try:
        ledger = Ledger.open(proc.get("ledger", ""))
    except (LedgerError, OSError):
        return {}
    try:
        return {"spend": ledger.spend(proc.get("task_id", "")),
                "caps": ledger.caps()}
    finally:
        ledger.close()


def pending_approvals(root: Path) -> list[dict]:
    root = Path(root)
    """Pending approval requests across all state dirs. Latest line
    per id wins; anything decided or superseded stays out."""
    pending: dict[str, dict] = {}
    for path in sorted(root.rglob("approvals.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rid = row.get("id", "")
            if not rid:
                continue
            if row.get("status") == "pending":
                row["_file"] = str(path)
                pending[rid] = row
            else:
                pending.pop(rid, None)
    return sorted(pending.values(), key=lambda r: r.get("ts", ""))


def decide_approval(root: Path, request_id: str, decision: str,
                    by: str = "operator") -> dict:
    root = Path(root)
    if decision not in ("approved", "denied"):
        raise KeyError(f"decision must be approved or denied, not {decision!r}")
    for row in pending_approvals(root):
        if row["id"] == request_id:
            line = {"id": request_id, "status": decision, "by": by,
                    "ts": _utcnow()}
            with open(row["_file"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, sort_keys=True) + "\n")
            return line
    raise KeyError(f"no pending approval {request_id!r}")


def kill_task(root: Path, task_id: str) -> dict:
    root = Path(root)
    """Stop a running task and refund its in-flight reservations.

    SIGTERM first, SIGKILL after the grace window, then a kill receipt
    and a ledger refund. Refuses anything not currently running: a
    second kill of the same task must not double-refund.
    """
    target = None
    for proc in find_tasks(root):
        if proc.get("task_id") == task_id:
            target = proc
            break
    if target is None:
        raise KeyError(f"unknown task {task_id!r}")
    if task_status(target) != "running":
        raise KeyError(f"task {task_id!r} is not running"
                       f" ({task_status(target)})")
    pid = target["pid"]
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + KILL_WAIT_SECONDS
    while time.time() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    used_kill = False
    if _pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
        used_kill = True
        deadline = time.time() + KILL_WAIT_SECONDS
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
    store = ReceiptStore(target["receipts"])
    store.append(agent_id="monitor", task_id=task_id, action="kill",
                 detail={"pid": pid,
                         "signal": "SIGKILL" if used_kill else "SIGTERM"},
                 outcome="ok")
    ledger = Ledger.open(target["ledger"])
    try:
        refund = ledger.refund_task(task_id)
    finally:
        ledger.close()
    proc_path = Path(target["_dir"]) / "proc.json"
    target["status"] = "killed"
    target["end_status"] = "killed"
    target["killed_at"] = _utcnow()
    target["refund"] = refund
    proc_path.write_text(json.dumps(
        {k: v for k, v in target.items() if not k.startswith("_")},
        indent=1), encoding="utf-8")
    return {"task_id": task_id, "pid": pid,
            "signal": "SIGKILL" if used_kill else "SIGTERM",
            "refund": refund}


def set_ledger_caps(ledger_path: str, **caps) -> dict:
    ledger = Ledger.open(ledger_path)
    try:
        return ledger.set_caps(**caps)
    finally:
        ledger.close()


def _page(title: str, body: str, refresh: int = 0) -> bytes:
    meta = (f'<meta http-equiv="refresh" content="{refresh}">' if refresh
            else "")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">{meta}
<title>{html.escape(title)} — Mini_AURION monitor</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:72em;margin:2em auto;
padding:0 1em;color:#222}}
table{{border-collapse:collapse;width:100%;margin:1em 0}}
th,td{{border:1px solid #ccc;padding:.3em .6em;text-align:left;
font-size:.9em}}
th{{background:#f4f4f4}}
nav a{{margin-right:1em}}
.bad{{color:#a00;font-weight:bold}}.ok{{color:#070}}.stale{{color:#a60}}
form{{display:inline}}
code{{font-size:.85em}}
</style></head><body>
<nav><a href="/">Live</a><a href="/experiments">Experiments</a>
<a href="/approvals">Approvals</a></nav>
<h1>{html.escape(title)}</h1>
{body}</body></html>""".encode("utf-8")


def _nano_usd(nano: int) -> str:
    return f"${nano / 1_000_000_000:.4f}"


def render_live(root: Path) -> bytes:
    tasks = find_tasks(root)
    rows = []
    for proc in tasks:
        tid = proc.get("task_id", "?")
        status = task_status(proc)
        last = last_receipt(proc.get("receipts", ""))
        age = receipt_age_seconds(last)
        money = task_spend(proc)
        spend = money.get("spend", {})
        caps = money.get("caps", {})
        seats = " ".join(
            f"{seat}={info.get('provider', '?')}/{info.get('model', '?')}"
            for seat, info in (proc.get("seats") or {}).items())
        tiers = ", ".join(f"{name} {_nano_usd(nano)}" for name, nano in
                          sorted(spend.get("by_provider", {}).items()))
        if last:
            seen = (f"{last.get('action','')} {last.get('tool','')}"
                    f" {last.get('outcome','')} (seq {last.get('seq','?')})")
        else:
            seen = "no receipts yet"
        flag = ""
        if status == "running" and age is not None \
                and age > STALE_SECONDS:
            flag = ' <span class="stale">STALE</span>'
        elif status == "died":
            flag = ' <span class="bad">DIED</span>'
        kill = (f'<form method="post" action="/kill">'
                f'<input type="hidden" name="task" value="{html.escape(tid)}">'
                f'<button type="submit">kill</button></form>'
                if status == "running" else "")
        rows.append(
            f"<tr><td><a href=\"/receipts?task={html.escape(tid)}\">"
            f"{html.escape(tid)}</a>{flag}</td>"
            f"<td>{html.escape(status)}</td>"
            f"<td>{html.escape(str(proc.get('pid','')))}</td>"
            f"<td>{html.escape(seen)}</td>"
            f"<td>{_nano_usd(spend.get('settled_nano', 0))}</td>"
            f"<td>{_nano_usd(spend.get('inflight_nano', 0))}</td>"
            f"<td>{html.escape(tiers or chr(8212))}</td>"
            f"<td>{html.escape(seats or '?')}</td>"
            f"<td>{_nano_usd(caps.get('task', 0))}"
            f'<form method="post" action="/caps">'
            f'<input type="hidden" name="ledger"'
            f' value="{html.escape(proc.get("ledger", ""))}">'
            f'<input type="text" name="task" size="12">'
            f"<button>set</button></form></td>"
            f"<td>{kill}</td></tr>")
    pend = pending_approvals(root)
    body = (f"<p>{len(tasks)} tasks, {len(pend)} pending approvals.</p>"
            "<table><tr><th>task</th><th>status</th><th>pid</th>"
            "<th>last receipt</th><th>settled</th><th>in-flight</th>"
            "<th>per-tier</th><th>seats</th>"
            "<th>task cap (nano, set)</th><th></th></tr>"
            + "".join(rows) + "</table>")
    return _page("Live", body, refresh=5)


def render_receipts(root: Path, task_id: str) -> bytes:
    target = next((p for p in find_tasks(root)
                   if p.get("task_id") == task_id), None)
    if target is None:
        return _page("Receipts", f"<p>unknown task {html.escape(task_id)}</p>")
    store = ReceiptStore(target["receipts"])
    rows = []
    try:
        anchor = json.loads(Path(str(target["receipts"]) + ".anchor")
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        anchor = {}
    indexed, anchored = store.index_count(), anchor.get("max_seq", 0)
    note = ""
    if anchored and indexed != anchored:
        note = ("<p>index stale "
                f"({indexed}/{anchored}); showing file tail.</p>")
    tail = store.index_tail(limit=50)
    if not tail:
        try:
            lines = Path(target["receipts"]).read_text(
                encoding="utf-8").strip().split("\n")
        except OSError:
            lines = []
        for line in lines[-50:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            tail.append({"seq": row.get("seq"), "action": row.get("action"),
                         "agent_id": row.get("agent_id"),
                         "tool": row.get("tool"),
                         "outcome": row.get("outcome"), "ts": row.get("ts")})
        tail.reverse()
    for row in tail:
        rows.append(
            f"<tr><td>{row.get('seq','')}</td>"
            f"<td>{html.escape(str(row.get('action','')))}</td>"
            f"<td>{html.escape(str(row.get('agent_id','')))}</td>"
            f"<td>{html.escape(str(row.get('tool','')))}</td>"
            f"<td>{html.escape(str(row.get('outcome','')))}</td>"
            f"<td>{html.escape(str(row.get('ts','')))}</td></tr>")
    body = (f"<p>task {html.escape(task_id)}, newest first.</p>{note}"
            "<table><tr><th>seq</th><th>action</th><th>agent</th>"
            "<th>tool</th><th>outcome</th><th>ts</th></tr>"
            + "".join(rows) + "</table>")
    return _page(f"Receipts — {task_id}", body)


def render_experiments(root: Path) -> bytes:
    rows = []
    for proc in find_tasks(root):
        tid = proc.get("task_id", "?")
        money = task_spend(proc)
        spend = money.get("spend", {})
        tiers = spend.get("by_provider", {})
        routing = (proc.get("routing") or "")[:24]
        rows.append(
            f"<tr><td><a href=\"/receipts?task={html.escape(tid)}\">"
            f"{html.escape(tid)}</a></td>"
            f"<td>{html.escape(task_status(proc))}</td>"
            f"<td>{html.escape(proc.get('end_status',''))}</td>"
            f"<td>{_nano_usd(spend.get('settled_nano', 0))}</td>"
            f"<td>{html.escape(json.dumps(tiers, sort_keys=True))}</td>"
            f"<td><code>{html.escape(routing)}</code></td></tr>")
    body = ("<table><tr><th>task</th><th>status</th><th>end</th>"
            "<th>settled</th><th>per-tier</th><th>routing</th></tr>"
            + "".join(rows) + "</table>")
    return _page("Experiments", body)


def render_approvals(root: Path) -> bytes:
    rows = []
    for row in pending_approvals(root):
        rid = row["id"]
        what = (f"{row.get('action_class','')} {row.get('tool','')} "
                f"{json.dumps(row.get('args', {}), sort_keys=True)[:200]}")
        rows.append(
            f"<tr><td>{html.escape(rid)}</td>"
            f"<td>{html.escape(row.get('task_id',''))}</td>"
            f"<td>{html.escape(what)}</td>"
            f"<td>{html.escape(row.get('reason',''))}</td>"
            f'<td><form method="post" action="/approve">'
            f'<input type="hidden" name="id" value="{html.escape(rid)}">'
            f'<input type="hidden" name="decision" value="approved">'
            f'<button type="submit">approve</button></form> '
            f'<form method="post" action="/approve">'
            f'<input type="hidden" name="id" value="{html.escape(rid)}">'
            f'<input type="hidden" name="decision" value="denied">'
            f'<button type="submit">deny</button></form></td></tr>')
    if not rows:
        return _page("Approvals", "<p>nothing pending.</p>")
    body = ("<table><tr><th>id</th><th>task</th><th>request</th>"
            "<th>reason</th><th></th></tr>" + "".join(rows) + "</table>")
    return _page("Approvals", body, refresh=5)


class Handler(BaseHTTPRequestHandler):
    root: Path = Path(".")
    server_version = "MiniAURIONMonitor/1"

    def log_message(self, *args):
        pass

    def _send(self, data: bytes, code: int = 200,
              content_type: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, where: str):
        data = f'<a href="{where}">done</a>'.encode()
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw).items() if v}

    def do_GET(self):  # noqa: N802 (http.server names the hook)
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            self._send(render_live(self.root))
        elif url.path == "/receipts":
            task = (query.get("task", [""])[0])
            self._send(render_receipts(self.root, task))
        elif url.path == "/experiments":
            self._send(render_experiments(self.root))
        elif url.path == "/approvals":
            self._send(render_approvals(self.root))
        else:
            self._send(b"not found", code=404, content_type="text/plain")

    def do_POST(self):  # noqa: N802 (http.server names the hook)
        url = urllib.parse.urlparse(self.path)
        form = self._form()
        try:
            if url.path == "/kill":
                kill_task(self.root, form.get("task", ""))
                self._redirect("/")
            elif url.path == "/approve":
                decide_approval(self.root, form.get("id", ""),
                                form.get("decision", ""))
                self._redirect("/approvals")
            elif url.path == "/caps":
                ledger_path = form.get("ledger", "")
                caps = {}
                for name in ("task", "daily", "global_"):
                    raw = form.get(name if name != "global_" else "global",
                                   "")
                    if raw.strip():
                        caps[name] = int(raw)
                if not ledger_path or not caps:
                    raise KeyError("ledger path and at least one cap")
                set_ledger_caps(ledger_path, **caps)
                self._redirect("/")
            else:
                self._send(b"not found", code=404, content_type="text/plain")
        except (KeyError, ValueError, LedgerError, OSError) as exc:
            self._send(f"refused: {exc}".encode(), code=400,
                       content_type="text/plain")


def serve(root: Path, port: int) -> ThreadingHTTPServer:
    root = Path(root)
    """Loopback only. There is no --host flag: an operator console
    that listens on anything else is a remote kill switch."""
    Handler.root = root
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Mini_AURION monitor")
    parser.add_argument("--root", required=True,
                        help="state root to watch (e.g. eval/tiers-smoke)")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    server = serve(Path(args.root), args.port)
    print(f"monitor on http://127.0.0.1:{server.server_port}/"
          f" watching {args.root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
