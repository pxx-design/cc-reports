#!/usr/bin/env python3
"""
cc-reports — Claude Code daily/weekly/monthly dashboard.

Usage:
  python3 cc-reports.py build         # one-shot build → cc-reports-data.json
  python3 cc-reports.py serve         # live server with click-to-refresh
  python3 cc-reports.py serve --port 8765

Config (optional): see config.example.json — drop a config.json next to this
file to merge project aliases / root-file rules. Without it the dashboard
falls back to the raw first-level directory name.

Data source: ~/.claude/projects/*/*.jsonl  (every cc session writes here)
Privacy:     100% local — no network calls except your own browser ↔ localhost.
"""

import argparse
import getpass
import glob
import importlib.util
import json
import os
import re
import socket
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.realpath(__file__))  # realpath: 经 symlink 安装为 skill 时也指向真实仓目录
HTML_PATH = os.path.join(ROOT, "cc-reports.html")
OUT_JSON = os.path.join(ROOT, "cc-reports-data.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")
PROJ_DIR = os.path.expanduser("~/.claude/projects")
DAYS = 30

sys.path.insert(0, os.path.dirname(ROOT))  # monorepo fallback: cursor1/ 共享内核
sys.path.insert(0, ROOT)                   # 优先用本仓 vendored 的 cc_usage_core(standalone)
from cc_usage_core import scan

# System local timezone — works wherever the user runs this
TZ = datetime.now().astimezone().tzinfo


def _current_user():
    """运行者本机用户名(顶栏显示用)。任何环境下都不抛异常。"""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "you"

# ─── Config ────────────────────────────────────────────
DEFAULT_CONFIG = {
    "project_aliases": {},        # {"old-name": "01-new-name"}
    "root_file_projects": [],     # [["regex", "logical-project-name"]]
    "cwd_overrides": {},          # {"/abs/path/to/cwd": "ProjectName"} — explicit
    "session_overrides": {},      # {"<sessionId>": "ProjectName"} — per-session, highest priority
    "fallback_projects": ["root files", "general", "claude-config"],  # 兜底桶(非真项目): 排名沉底+不进主线叙事
}

# Build manifests — strong signal that cwd IS a single project root.
# .git / .hg / .svn are INTENTIONALLY excluded — workspaces / mega-repos
# also use version control, so VCS presence alone is too weak a signal.
PROJECT_ROOT_MARKERS = [
    "package.json", "Cargo.toml", "pyproject.toml",
    "setup.py", "setup.cfg",
    "go.mod", "Gemfile", "pom.xml",
    "build.gradle", "build.gradle.kts",
    "requirements.txt", "Pipfile", "poetry.lock",
    "Makefile", "CMakeLists.txt",
    "Podfile",
]

_root_check_cache = {}
def is_project_root(cwd):
    """Returns True if cwd contains any version-control or build-manifest marker.
    Result cached per cwd to avoid repeated stat() calls during aggregation."""
    if not cwd:
        return False
    if cwd in _root_check_cache:
        return _root_check_cache[cwd]
    result = False
    if os.path.isdir(cwd):
        for m in PROJECT_ROOT_MARKERS:
            if os.path.exists(os.path.join(cwd, m)):
                result = True
                break
    _root_check_cache[cwd] = result
    return result

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            for k in cfg:
                if k in user:
                    cfg[k] = user[k]
        except Exception as e:
            print(f"[warn] failed to load config.json: {e}", file=sys.stderr)
    # compile root-file regexes
    cfg["_root_file_compiled"] = [
        (re.compile(pat), name) for pat, name in cfg.get("root_file_projects", [])
    ]
    return cfg


CFG = load_config()


def is_fallback(name):
    """兜底桶(root files / general / claude-config 等非真项目, 可经 config 扩充)。
    这些不是真项目, 排名沉底、不当主线叙事的主角。"""
    return name in CFG.get("fallback_projects", [])


# ─── Helpers ───────────────────────────────────────────
def label_cwd(cwd):
    if not cwd:
        return "—"
    home = os.path.expanduser("~")
    short = cwd.replace(home, "~")
    parts = [p for p in short.split("/") if p]
    if len(parts) <= 2:
        return short
    return ".../" + "/".join(parts[-2:])


parse_ts = scan.parse_ts  # 时间戳解析统一走共享内核 (逐字相同实现)


def path_to_project(path, session_cwd):
    """Map a tool_use file_path to a project name, relative to the session's cwd.

    - If path is inside session_cwd, the first-level directory becomes the project.
    - Aliases from config.json are applied for legacy names.
    - Files directly in session_cwd → matched against config.root_file_projects,
      else "root files".
    - Files outside the session's cwd → None (don't count).
    """
    if not path or not isinstance(path, str) or not session_cwd:
        return None
    root = session_cwd.rstrip("/") + "/"
    if not path.startswith(root):
        return None
    rel = path[len(root):]
    if not rel:
        return None
    parts = rel.split("/")
    first = parts[0]

    if len(parts) == 1:
        for pat, name in CFG["_root_file_compiled"]:
            if pat.match(first):
                return name
        return "root files"

    if first == ".claude":
        return "claude-config"
    aliases = CFG.get("project_aliases", {})
    if first in aliases:
        return aliases[first]
    return first


def project_for_session(session_cwd, project_counts, sid=None):
    """Decide the primary project for a session-day slice.

    Priority (highest first):
      0. session_overrides config — user pins a specific sessionId to a project
         (covers cases where the same session jumps between abstract names
         like 'src' / 'claude-config' / 'general' across days)
      1. cwd_overrides config — user explicitly maps an absolute cwd to a name
      2. cwd is a single project root (build manifest present) → cwd basename
      3. cwd is a workspace containing multiple projects → most-touched
         first-level subdir from tool_use file_paths
      4. No file_path data at all → "general"
    """
    aliases = CFG.get("project_aliases", {})

    # 0. Per-session override (highest)
    session_map = CFG.get("session_overrides", {})
    if sid and sid in session_map:
        name = session_map[sid]
        return aliases.get(name, name)

    # 1. Explicit cwd override
    overrides = CFG.get("cwd_overrides", {})
    if session_cwd and session_cwd in overrides:
        return overrides[session_cwd]

    # 2. Single-project cwd → use cwd basename
    if session_cwd and is_project_root(session_cwd):
        name = os.path.basename(session_cwd.rstrip("/"))
        return aliases.get(name, name)

    # 3. Multi-project workspace → most-touched first-level dir
    if project_counts:
        return max(project_counts.items(), key=lambda kv: kv[1])[0]

    # 4. Nothing to go on
    return "general"


def _empty_slice():
    return {
        "user_msgs": 0,
        "assistant_msgs": 0,
        "tool_uses": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0},
        "models": defaultdict(int),
        "first_ts": None,
        "last_ts": None,
        "event_ts": [],            # 所有事件时间戳, 用于算「活跃时长」(相邻间隔累加, 单段封顶)
        "hourly": [0] * 24,
        "hourly_by_model": [defaultdict(int) for _ in range(24)],
        "hourly_tokens": [0] * 24,
        "hourly_tokens_by_model": [defaultdict(int) for _ in range(24)],
        "project_counts": defaultdict(int),
    }


# ─── Aggregation ───────────────────────────────────────
def build_data():
    """Scan all jsonl files and aggregate per-day slices.

    A session that spans multiple local-tz days produces one slice per day.
    Tool_use file_paths are interpreted relative to that session's own cwd,
    so this script works for any user without hard-coded project roots.
    """
    files = scan.find_jsonl(PROJ_DIR, recursive=False)

    session_meta = {}              # sid -> {title, cwd, branch}
    slices = {}                    # (date_iso, sid) -> slice

    def get_slice(date_iso, sid):
        key = (date_iso, sid)
        if key not in slices:
            slices[key] = _empty_slice()
        return slices[key]

    for rec in scan.iter_records(files):
        sid = rec.get("sessionId")
        if not sid:
            continue
        if sid not in session_meta:
            session_meta[sid] = {"title": None, "first_prompt": None, "cwd": None, "branch": None}
        meta = session_meta[sid]
        t = rec.get("type")

        if t == "ai-title":
            meta["title"] = rec.get("aiTitle") or meta["title"]
            continue

        ts = parse_ts(rec.get("timestamp"))
        if ts is None:
            continue
        local = ts.astimezone(TZ)
        date_iso = local.date().isoformat()
        sl = get_slice(date_iso, sid)
        if sl["first_ts"] is None or ts < sl["first_ts"]:
            sl["first_ts"] = ts
        if sl["last_ts"] is None or ts > sl["last_ts"]:
            sl["last_ts"] = ts
        sl["event_ts"].append(ts)

        if t == "user":
            sl["user_msgs"] += 1
            if rec.get("cwd"):
                meta["cwd"] = rec["cwd"]
            if rec.get("gitBranch"):
                meta["branch"] = rec["gitBranch"]
            # 无 ai-title 时用首条真实 prompt 兜底 (headless 桥接会话靠这个自识别)
            if meta.get("first_prompt") is None:
                content = (rec.get("message") or {}).get("content")
                txt = None
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    parts = [c.get("text") for c in content
                             if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
                    if parts:
                        txt = " ".join(parts)
                if txt:
                    txt = txt.strip()
                    if txt and not txt.startswith(("This session is being continued", "Caveat:")):
                        meta["first_prompt"] = re.sub(r"\s+", " ", txt)[:46]
        elif t == "assistant":
            sl["assistant_msgs"] += 1
            sl["hourly"][local.hour] += 1
            msg = rec.get("message") or {}
            model = msg.get("model")
            if model:
                sl["models"][model] += 1
                sl["hourly_by_model"][local.hour][model] += 1
            usage = msg.get("usage") or {}
            u_in = int(usage.get("input_tokens") or 0)
            u_out = int(usage.get("output_tokens") or 0)
            u_cr = int(usage.get("cache_read_input_tokens") or 0)
            u_cw = int(usage.get("cache_creation_input_tokens") or 0)
            sl["tokens"]["input"] += u_in
            sl["tokens"]["output"] += u_out
            sl["tokens"]["cache_read"] += u_cr
            sl["tokens"]["cache_creation"] += u_cw
            # per-hour token consumption (total) — powers the activity heatmap/bar
            msg_tok = u_in + u_out + u_cr + u_cw
            sl["hourly_tokens"][local.hour] += msg_tok
            if model:
                sl["hourly_tokens_by_model"][local.hour][model] += msg_tok
            for c in (msg.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    sl["tool_uses"] += 1
                    inp = c.get("input") or {}
                    fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
                    proj = path_to_project(fp, meta.get("cwd"))
                    if proj:
                        sl["project_counts"][proj] += 1

    for sl in slices.values():
        tk = sl["tokens"]
        tk["total"] = tk["input"] + tk["output"] + tk["cache_read"] + tk["cache_creation"]

    by_date = defaultdict(list)
    for (date_iso, sid), sl in slices.items():
        by_date[date_iso].append((sid, sl))

    today = datetime.now(TZ).date()
    days_out = []
    for i in range(DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.isoformat()
        day_slices = by_date.get(key, [])

        hourly = [0] * 24
        hourly_by_model = [defaultdict(int) for _ in range(24)]
        hourly_tokens = [0] * 24
        hourly_tokens_by_model = [defaultdict(int) for _ in range(24)]
        tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
        models = defaultdict(int)
        projects = defaultdict(lambda: {"sessions": 0, "tokens": 0, "msgs": 0, "active_min": 0})
        sessions_list = []
        user_total = assistant_total = tool_uses_total = 0

        for sid, sl in day_slices:
            user_total += sl["user_msgs"]
            assistant_total += sl["assistant_msgs"]
            tool_uses_total += sl["tool_uses"]
            for k in tokens:
                tokens[k] += sl["tokens"].get(k, 0)
            for m, c in sl["models"].items():
                models[m] += c
            for hi in range(24):
                hourly[hi] += sl["hourly"][hi]
                for m, c in sl["hourly_by_model"][hi].items():
                    hourly_by_model[hi][m] += c
                hourly_tokens[hi] += sl["hourly_tokens"][hi]
                for m, c in sl["hourly_tokens_by_model"][hi].items():
                    hourly_tokens_by_model[hi][m] += c

            meta = session_meta.get(sid, {})
            primary = project_for_session(meta.get("cwd"), dict(sl["project_counts"]), sid=sid)
            start_local = sl["first_ts"].astimezone(TZ) if sl["first_ts"] else None
            end_local = sl["last_ts"].astimezone(TZ) if sl["last_ts"] else None
            # 活跃时长: 相邻事件间隔累加, 单段挂机封顶 5 分钟 (排除离开键盘的空窗,
            # 取代旧的 wall-clock end-start —— 后者含 idle 又跨 session 累加, 会算出单日 54h)
            tss = sorted(sl["event_ts"])
            active_sec = 0
            for a_ts, b_ts in zip(tss, tss[1:]):
                gap = (b_ts - a_ts).total_seconds()
                if gap > 0:
                    active_sec += min(gap, 300)
            active_min = int(active_sec // 60)

            projects[primary]["sessions"] += 1
            projects[primary]["tokens"] += sl["tokens"]["total"]
            projects[primary]["msgs"] += sl["user_msgs"] + sl["assistant_msgs"]
            projects[primary]["active_min"] += active_min

            sessions_list.append({
                "id": sid,
                "title": meta.get("title") or meta.get("first_prompt") or "(untitled)",
                "start": start_local.isoformat() if start_local else None,
                "end": end_local.isoformat() if end_local else None,
                "active_min": active_min,
                "tokens": sl["tokens"]["total"],
                "branch": meta.get("branch") or "",
                "cwd_label": label_cwd(meta.get("cwd") or "—"),
                "project": primary,
            })

        sessions_list.sort(key=lambda x: x["start"] or "")
        proj_list = sorted(
            [{"name": n, "label": n, "fallback": is_fallback(n), **info} for n, info in projects.items()],
            key=lambda x: (x["fallback"], -x["tokens"]),
        )

        days_out.append({
            "date": key,
            "weekday": d.strftime("%a"),
            "sessions": len(day_slices),
            "user_msgs": user_total,
            "assistant_msgs": assistant_total,
            "tool_uses": tool_uses_total,
            "tokens": tokens,
            "models": dict(models),
            "projects": proj_list,
            "hourly": hourly,
            "hourly_by_model": [dict(h) for h in hourly_by_model],
            "hourly_tokens": hourly_tokens,
            "hourly_tokens_by_model": [dict(h) for h in hourly_tokens_by_model],
            "sessions_list": sessions_list,
        })

    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "tz": str(TZ),
        "today": today.isoformat(),
        "user": _current_user(),  # 顶栏显示运行者本机用户名,谁跑就是谁
        "days": days_out,
    }


# ─── Session anatomy (click-to-drill) ──────────────────
def build_session_anatomy(sid, date_iso=None):
    """单个 session 的逐轮解剖（点击下钻用）。

    一"轮" = 一条真实 user prompt + 它触发的 assistant 工作，直到下一条 prompt。
    每轮产出: prompt 首句 + token 成本 + 工具计数 + 碰过的文件。
    **只回用户自己的 prompt 行 + 元数据，绝不回 Claude 正文 / 文件内容。**
    """
    matches = glob.glob(os.path.join(PROJ_DIR, "*", sid + ".jsonl"))
    if not matches:
        return {"error": "session not found", "sid": sid, "turns": []}
    path = matches[0]

    def first_line(text, n=90):
        text = re.sub(r"\s+", " ", (text or "").strip())
        return text[:n]

    def clean_prompt(text):
        # 把系统注入的 prompt 文本换成短标签, 否则首句对用户无意义
        t = (text or "").lstrip()
        if t.startswith("This session is being continued") or t.startswith("Caveat:"):
            return "⟳ 续接上文（compact 后继续）"
        if t[:7].lower().startswith("[image"):
            return "🖼 [贴图]"
        return first_line(text)

    title = None
    turns = []
    cur = None

    def flush():
        nonlocal cur
        if cur is not None:
            turns.append(cur)
            cur = None

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            t = rec.get("type")
            if t == "ai-title":
                title = rec.get("aiTitle") or title
                continue
            ts = parse_ts(rec.get("timestamp"))
            local = ts.astimezone(TZ) if ts else None

            if t == "user":
                msg = rec.get("message") or {}
                content = msg.get("content")
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [c.get("text") for c in content
                             if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
                    if parts:
                        text = "\n".join(parts)
                if text is None:
                    continue  # tool_result / meta — 不是真 prompt, 不作轮次边界
                flush()
                cur = {
                    "time": local.strftime("%H:%M") if local else "",
                    "date": local.date().isoformat() if local else None,
                    "prompt": clean_prompt(text),
                    "tokens": 0,
                    "tools": defaultdict(int),
                    "files": [],
                    "_seen": set(),
                }
            elif t == "assistant" and cur is not None:
                msg = rec.get("message") or {}
                usage = msg.get("usage") or {}
                cur["tokens"] += (int(usage.get("input_tokens") or 0)
                                  + int(usage.get("output_tokens") or 0)
                                  + int(usage.get("cache_read_input_tokens") or 0)
                                  + int(usage.get("cache_creation_input_tokens") or 0))
                for c in (msg.get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        cur["tools"][c.get("name") or "tool"] += 1
                        inp = c.get("input") or {}
                        fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
                        base = os.path.basename(fp) if fp else ""
                        if base and base not in cur["_seen"]:
                            cur["_seen"].add(base)
                            cur["files"].append(base)
    flush()

    out = []
    for tn in turns:
        if date_iso and tn["date"] != date_iso:
            continue
        out.append({
            "time": tn["time"],
            "prompt": tn["prompt"] or "(无文本)",
            "tokens": tn["tokens"],
            "tools": dict(tn["tools"]),
            "files": tn["files"][:6],
        })
    return {
        "sid": sid,
        "date": date_iso,
        "title": title or "(untitled)",
        "turns": out,
        "total_tokens": sum(t["tokens"] for t in out),
    }


# ─── HTTP Server ───────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} - "
                         + (fmt % args) + "\n")

    def _send(self, status, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html", "/cc-reports.html"):
            try:
                with open(HTML_PATH, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "cc-reports.html not found", "text/plain")
            return

        if path == "/api/data":
            t0 = time.time()
            try:
                data = build_data()
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(data, ensure_ascii=False),
                       "application/json; charset=utf-8",
                       {"X-Build-Ms": str(int((time.time() - t0) * 1000))})
            return

        if path == "/api/session":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            sid = (qs.get("sid") or [""])[0]
            date_iso = (qs.get("date") or [None])[0]
            if not re.match(r"^[A-Za-z0-9_-]+$", sid):
                self._send(400, json.dumps({"error": "bad sid"}), "application/json; charset=utf-8")
                return
            try:
                data = build_session_anatomy(sid, date_iso)
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
            return

        safe = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
        if not safe.startswith(ROOT) or not os.path.isfile(safe):
            self._send(404, "not found", "text/plain")
            return
        ctype = "text/plain"
        if safe.endswith(".json"): ctype = "application/json"
        elif safe.endswith(".css"): ctype = "text/css"
        elif safe.endswith(".js"): ctype = "application/javascript"
        elif safe.endswith(".svg"): ctype = "image/svg+xml"
        with open(safe, "rb") as fh:
            self._send(200, fh.read(), ctype)


def find_free_port(start=8765, max_tries=20):
    for p in range(start, start + max_tries):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", p))
                return p
        except OSError:
            continue
    return None


# ─── CLI ───────────────────────────────────────────────
def cmd_build(args):
    out = build_data()
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    today = out["days"][-1]
    print(f"wrote {OUT_JSON}")
    print(f"today {today['date']} ({today['weekday']}): "
          f"{today['sessions']} sessions, "
          f"{today['user_msgs']} prompts, "
          f"{today['tokens']['total']:,} tokens, "
          f"{len(today['projects'])} projects")


def cmd_serve(args):
    port = args.port
    if port is None:
        port = find_free_port(8765)
        if port is None:
            print("[error] no free port in 8765-8784", file=sys.stderr)
            sys.exit(1)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[error] port {port} is in use: {e}", file=sys.stderr)
        sys.exit(1)
    url = f"http://localhost:{port}/"
    print(f"cc-reports · {url}")
    print(f"  config:   {'loaded ' + CONFIG_PATH if os.path.exists(CONFIG_PATH) else 'none (using defaults)'}")
    print(f"  tz:       {TZ}")
    print(f"  data:     {url}api/data (rebuilt on every request)")
    print("  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code daily/weekly/monthly dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="aggregate to cc-reports-data.json (one-shot)")
    sp = sub.add_parser("serve", help="live HTTP server")
    sp.add_argument("--port", type=int, default=None,
                    help="port to bind (default: auto-pick from 8765+)")
    args = parser.parse_args()
    if args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "serve":
        cmd_serve(args)


if __name__ == "__main__":
    main()
