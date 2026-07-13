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
import subprocess
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
from cc_usage_core.models import price_of, pricing_dict

# System local timezone — works wherever the user runs this
TZ = datetime.now().astimezone().tzinfo


def _placeholder_name(s):
    """占位符 / 无意义名判断:空、常见占位、纯邮箱。"""
    if not s:
        return True
    low = s.strip().lower()
    if low in ("your name", "user", "username", "unknown", "you", "name", "admin"):
        return True
    # 纯邮箱(无空格且含 @) 当占位
    if "@" in low and " " not in low and "." in low.split("@")[-1]:
        return True
    return False


def _login_name():
    """本机登录账户名。"""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or ""


def _git_user_name():
    """全局 git 身份 — 开发者自起的名,最贴 CC 场景。"""
    try:
        out = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return "" if _placeholder_name(out) else out
    except Exception:
        return ""


def _os_full_name():
    """系统全名(GECOS / macOS 全名),排除等于登录名或占位的情况。"""
    login = _login_name()
    candidates = []
    try:
        import pwd
        candidates.append(pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip())
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            candidates.append(subprocess.run(
                ["id", "-F"], capture_output=True, text=True, timeout=2,
            ).stdout.strip())
        except Exception:
            pass
    for full in candidates:
        if full and not _placeholder_name(full) and full != login:
            return full
    return ""


def _current_user(cfg=None):
    """顶栏显示名。回退链:config.display_name → git 身份 → 系统全名 → 登录名 → 'you'。
    任何环境下都不抛异常。"""
    # 1. 显式配置(最高)
    if cfg:
        dn = (cfg.get("display_name") or "").strip()
        if dn:
            return dn
    # 2. git 身份
    name = _git_user_name()
    if name:
        return name
    # 3. 系统全名
    name = _os_full_name()
    if name:
        return name
    # 4. 登录名(现状兜底)
    name = _login_name()
    if name:
        return name
    # 5. 最终兜底
    return "you"

# ─── Config ────────────────────────────────────────────
DEFAULT_CONFIG = {
    "display_name": "",           # 顶栏显示名;留空则走 git→系统全名→登录名 回退链
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
    "Package.swift",
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


# 临时产物路径:harness scratchpad / 系统临时目录 —— 不算项目工作,不参与归类。
# 只匹配前缀 /tmp、/private/tmp、macOS 真 TMPDIR /var/folders/…;不全局匹配
# "/scratchpad/",否则会误伤用户真实的、名叫 scratchpad 的项目子目录。
_TMP_RX = re.compile(r"^(/private)?(/tmp(/|$)|/var/folders/)")


def path_to_project(path, session_cwd, anchor=None):
    """Map a tool_use file_path to a project name.

    Resolve against the session's **launch anchor** first (the cwd at session
    start, stable across mid-session `cd`), then the running cwd as fallback.
    This is what makes `cd 09-xhs; edit 09-xhs/foo.html` count as 09-xhs instead
    of leaking into the "root files" bucket just because you walked into the dir.

    - Temp / scratchpad paths → None (harness artifacts, not project work).
    - First-level directory under the base becomes the project (aliased). Because
      the anchor is tried first, `cd`-ing into a subdir and editing files there
      still resolves via the workspace-relative first dir.
    - A single loose file directly under the base → config.root_file_projects
      match, else "root files". (Crediting a bare project dir launched *into*
      by name is a layout-model concern, deliberately out of scope here.)
    - `.claude/…` → "claude-config".
    - Path under none of the bases → None (don't count).
    """
    if not path or not isinstance(path, str):
        return None
    if _TMP_RX.search(path):
        return None
    aliases = CFG.get("project_aliases", {})
    for base in (anchor, session_cwd):
        if not base:
            continue
        root = base.rstrip("/") + "/"
        if not path.startswith(root):
            continue
        rel = path[len(root):]
        if not rel:
            continue
        parts = rel.split("/")
        first = parts[0]

        if len(parts) == 1:
            for pat, name in CFG["_root_file_compiled"]:
                if pat.match(first):
                    return name
            return "root files"

        if first == ".claude":
            return "claude-config"
        return aliases.get(first, first)
    return None


def project_for_session(session_cwd, project_counts, sid=None, anchor=None):
    """Decide the primary project for a session-day slice.

    Priority (highest first):
      0. session_overrides config — user pins a specific sessionId to a project
      1. cwd_overrides config — user maps an absolute cwd to a name
      2. current cwd / launch anchor is a single project root (build manifest
         present) → basename
      3. workspace with multiple projects → most-touched **real** project from
         tool_use file_paths. Fallback buckets (root files / claude-config / …)
         are barred from *winning* the vote — they only settle it when no real
         project got a single touch. This stops a pile of loose screenshots at
         the workspace root from out-voting the actual project you worked on.
      4. No file_path data at all → "general"

    For 1 & 2 the running cwd is tried **before** the launch anchor: where you
    ended up working is more specific than where you started, so launching inside
    a build-manifest project A and then `cd`-ing to project B credits B, not A.
    """
    aliases = CFG.get("project_aliases", {})
    # cwd first, anchor second (more-specific wins); dedupe when they're equal
    bases = list(dict.fromkeys(b for b in (session_cwd, anchor) if b))

    # 0. Per-session override (highest)
    session_map = CFG.get("session_overrides", {})
    if sid and sid in session_map:
        name = session_map[sid]
        return aliases.get(name, name)

    # 1. Explicit cwd override
    overrides = CFG.get("cwd_overrides", {})
    for base in bases:
        if base in overrides:
            return overrides[base]

    # 2. Single-project root → basename
    for base in bases:
        if is_project_root(base):
            name = os.path.basename(base.rstrip("/"))
            return aliases.get(name, name)

    # 3. Multi-project workspace → most-touched REAL project (fallbacks can't win)
    if project_counts:
        real = {k: v for k, v in project_counts.items() if not is_fallback(k)}
        if real:
            return max(real.items(), key=lambda kv: kv[1])[0]
        return max(project_counts.items(), key=lambda kv: kv[1])[0]

    # 4. Nothing to go on
    return "general"


def _empty_slice():
    return {
        "user_msgs": 0,
        "assistant_msgs": 0,
        "tool_uses": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0},
        "cost": 0.0,               # 等效 API 成本($, 按模型单价累加; GLM/未知不计)
        # 按模型分桶的四类 token 量 —— 喂点击弹层的成本明细; 单价前端用 pricing 查(单一源)
        "cost_by_model": defaultdict(lambda: {"in": 0, "out": 0, "cw": 0, "cr": 0}),
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


# ─── Capability utilization (装了却没用) ────────────────
# "你亲手装的能力":个人 skills(~/.claude/skills/*/SKILL.md) + 用户 config 里的 MCP。
# 跟近 N 天真实调用比,算出闲置清单。插件捆绑的(lark-* 等)不计入,免得淹没信号。
def _installed_capabilities():
    home = os.path.expanduser("~")
    skills = []
    sk_dir = os.path.join(home, ".claude", "skills")
    if os.path.isdir(sk_dir):
        for name in sorted(os.listdir(sk_dir)):
            p = os.path.join(sk_dir, name)
            if not os.path.isfile(os.path.join(p, "SKILL.md")):
                continue
            # 排除软链进捆绑库(.agents/skills/,如 lark-* 一大坨)的——不算"你亲手装的"
            if "/.agents/skills/" in os.path.realpath(p):
                continue
            skills.append(name)
    mcp = set()
    for cfg in [os.path.join(home, ".claude.json"),
                os.path.join(home, ".claude", "settings.json")]:
        try:
            with open(cfg, encoding="utf-8") as fh:
                mcp.update((json.load(fh).get("mcpServers") or {}).keys())
        except Exception:
            pass
    return skills, sorted(mcp)


def _used_capabilities(window_days=30):
    """近 N 天真实调用过的 MCP server / skill(含子 agent 会话里的调用)。"""
    cut = time.time() - window_days * 86400
    used_mcp, used_skills = set(), set()
    for f in glob.glob(os.path.join(PROJ_DIR, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(f) < cut:
                continue
        except OSError:
            continue
        try:
            fh = open(f, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                content = (rec.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for p in content:
                    if not isinstance(p, dict) or p.get("type") != "tool_use":
                        continue
                    name = p.get("name", "")
                    inp = p.get("input") or {}
                    if name.startswith("mcp__"):
                        parts = name.split("__")
                        if len(parts) > 1:
                            used_mcp.add(parts[1])
                    elif name == "Skill":
                        s = inp.get("skill", "")
                        if s:
                            used_skills.add(s)          # 个人 skill 无命名空间,可直接交集
    return used_mcp, used_skills


def compute_utilization(window_days=30):
    skills_inst, mcp_inst = _installed_capabilities()
    used_mcp, used_skills = _used_capabilities(window_days)

    def part(installed, used):
        inst = list(installed)
        u = [x for x in inst if x in used]
        idle = [x for x in inst if x not in used]
        return {"installed": len(inst), "used": len(u),
                "idle": idle, "used_names": sorted(u)}

    return {
        "window_days": window_days,
        "mcp": part(mcp_inst, used_mcp),
        "skills": part(skills_inst, used_skills),
    }


# ─── Aggregation ───────────────────────────────────────
def build_data(with_utilization=False):
    """Scan all jsonl files and aggregate per-day slices.

    A session that spans multiple local-tz days produces one slice per day.
    Tool_use file_paths are interpreted relative to that session's own cwd,
    so this script works for any user without hard-coded project roots.
    """
    global CFG
    CFG = load_config()  # 每次请求重读,config.json 改动刷新即生效(不必重启)
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
            session_meta[sid] = {"title": None, "first_prompt": None, "cwd": None,
                                 "anchor": None, "branch": None}
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
                # launch anchor = first real cwd of the session (temp dirs skipped)
                if meta["anchor"] is None and not _TMP_RX.search(rec["cwd"]):
                    meta["anchor"] = rec["cwd"]
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
            # 等效 API 成本($): 单价 (input, output, cache_write, cache_read)/1M; GLM/未知 price=None 不计
            base = model.split("[", 1)[0] if model else None
            pr = price_of(base) if base else None
            if pr:
                sl["cost"] += (u_in * pr[0] + u_out * pr[1] + u_cw * pr[2] + u_cr * pr[3]) / 1_000_000
                cbm = sl["cost_by_model"][base]
                cbm["in"] += u_in; cbm["out"] += u_out; cbm["cw"] += u_cw; cbm["cr"] += u_cr
            for c in (msg.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    sl["tool_uses"] += 1
                    inp = c.get("input") or {}
                    fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
                    proj = path_to_project(fp, meta.get("cwd"), meta.get("anchor"))
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
        cost = 0.0
        cost_by_model = defaultdict(lambda: {"in": 0, "out": 0, "cw": 0, "cr": 0})
        models = defaultdict(int)
        projects = defaultdict(lambda: {"sessions": 0, "tokens": 0, "output": 0,
                                        "cache_creation": 0, "msgs": 0, "active_min": 0})
        sessions_list = []
        user_total = assistant_total = tool_uses_total = 0

        for sid, sl in day_slices:
            user_total += sl["user_msgs"]
            assistant_total += sl["assistant_msgs"]
            tool_uses_total += sl["tool_uses"]
            for k in tokens:
                tokens[k] += sl["tokens"].get(k, 0)
            cost += sl.get("cost", 0.0)
            for b, td in sl["cost_by_model"].items():
                agg = cost_by_model[b]
                for k in ("in", "out", "cw", "cr"):
                    agg[k] += td[k]
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
            primary = project_for_session(meta.get("cwd"), dict(sl["project_counts"]),
                                          sid=sid, anchor=meta.get("anchor"))
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
            projects[primary]["output"] += sl["tokens"]["output"]
            projects[primary]["cache_creation"] += sl["tokens"]["cache_creation"]
            projects[primary]["msgs"] += sl["user_msgs"] + sl["assistant_msgs"]
            projects[primary]["active_min"] += active_min

            sessions_list.append({
                "id": sid,
                "title": meta.get("title") or meta.get("first_prompt") or "(untitled)",
                "start": start_local.isoformat() if start_local else None,
                "end": end_local.isoformat() if end_local else None,
                "active_min": active_min,
                "tokens": sl["tokens"]["total"],
                "work": sl["tokens"]["output"] + sl["tokens"]["cache_creation"],
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
            "cost": round(cost, 4),
            "cost_by_model": {b: dict(v) for b, v in cost_by_model.items()},
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
        "user": _current_user(CFG),  # 顶栏显示名:config→git→系统全名→登录名 回退
        # 单价表(单一源=models.py): {model_id: [in, out, cache_write, cache_read]}/1M
        # 前端成本明细直接读它算钱 —— 改价/加模型只改 models.py 一处, 网页自动跟
        "pricing": pricing_dict(),
        "days": days_out,
        "utilization": compute_utilization() if with_utilization else None,
    }


# ─── Session anatomy (click-to-drill) ──────────────────
def build_session_anatomy(sid, date_iso=None):
    """单个 session 的逐轮解剖（点击下钻用）。

    一"轮" = 一条真实 user prompt + 它触发的 assistant 工作，直到下一条 prompt。
    每轮产出: prompt 首句 + token 成本 + 工具计数 + 碰过的文件。
    **只回用户自己的 prompt 行 + 元数据，绝不回 Claude 正文 / 文件内容。**
    """
    global CFG
    CFG = load_config()  # 与 build_data 一致:每次请求重读 config
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
                data = build_data(with_utilization=True)
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


def cmd_glance(args):
    """常驻浮层用的精简快照：只回今天(或近 N 天)的用量 + top 项目。
    输出紧凑 JSON 到 stdout，供菜单栏 app 每次刷新时读取。"""
    data = build_data()
    days = data.get("days", [])
    win = max(1, int(getattr(args, "days", 1) or 1))
    sel = days[-win:] if days else []

    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
    sessions = 0
    proj_agg = {}  # name -> {tokens, active_min, sessions, fallback}
    for d in sel:
        for k in tokens:
            tokens[k] += d["tokens"].get(k, 0)
        sessions += d.get("sessions", 0)
        for p in d.get("projects", []):
            a = proj_agg.setdefault(p["name"], {"tokens": 0, "output": 0, "cache_creation": 0,
                                                "active_min": 0, "sessions": 0,
                                                "fallback": p.get("fallback", False)})
            a["tokens"] += p.get("tokens", 0)
            a["output"] += p.get("output", 0)
            a["cache_creation"] += p.get("cache_creation", 0)
            a["active_min"] += p.get("active_min", 0)
            a["sessions"] += p.get("sessions", 0)

    # 每项目近 7 天 work(=output+cache_creation) 日序列,喂浮窗 sparkline(oldest→newest;缺席天补 0)
    last7 = days[-7:]
    spark = {
        name: [
            next((pp.get("output", 0) + pp.get("cache_creation", 0)
                  for pp in d.get("projects", []) if pp["name"] == name), 0)
            for d in last7
        ]
        for name in proj_agg
    }

    projects = sorted(
        [{"name": n, **v, "work": v["output"] + v["cache_creation"], "spark": spark.get(n, [])}
         for n, v in proj_agg.items()],
        key=lambda x: (x["fallback"], -x["work"]),
    )
    active_min = sum(p["active_min"] for p in projects)

    # classification health: how much of the window's work landed in fallback
    # buckets, and whether a fallback bucket is the single biggest by work.
    # When either trips, the widget stops dimming it and raises a visible warning
    # instead — a misclassified pile should read as "look here", not "ignore me".
    # Gate on an absolute work floor so a pure-chat day (only "general", tiny work)
    # doesn't scream "100% unclassified".
    WARN_MIN_WORK = 100_000
    real_tot = sum(p["work"] for p in projects)
    tot_work = real_tot or 1
    fb_work = sum(p["work"] for p in projects if p["fallback"])
    by_work = sorted(projects, key=lambda x: -x["work"])
    fb_share = fb_work / tot_work
    fb_top = by_work[0]["name"] if by_work and by_work[0]["fallback"] else None
    warn = real_tot >= WARN_MIN_WORK and (bool(fb_top) or fb_share > 0.30)
    classify = {
        "fallback_share": round(fb_share, 3),
        "fallback_top": fb_top,                        # name of #1 bucket iff it's fallback
        "warn": warn,
    }
    # normal order sinks fallbacks to the bottom (calm); but when we're warning,
    # the offending bucket IS the story — order by work so it can't be sliced out.
    ordered = by_work if warn else projects

    today = days[-1] if days else {}
    out = {
        "generated_at": data.get("generated_at"),
        "range": {"days": win, "start": sel[0]["date"] if sel else None,
                  "end": sel[-1]["date"] if sel else None},
        "tokens": {**tokens, "work": tokens["output"] + tokens["cache_creation"]},
        "active_min": active_min,
        "sessions": sessions,
        "classify": classify,
        "projects": ordered[:8],
        # 24H 视图:今日每小时 token 消耗(节奏形状用)
        "hourly": today.get("hourly_tokens", [0] * 24),
        # TIME 视图折线:近 7 天每日活跃分钟
        "active7": [sum(p.get("active_min", 0) for p in d.get("projects", [])) for d in last7],
    }
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def cmd_doctor(args):
    """Self-serve classification triage.

    Finds sessions whose work landed in a fallback bucket despite touching what
    looks like a real project, guesses the intended project, and prints a
    ready-to-paste config rule. Prints a clean bill of health when there's
    nothing to fix — so a user with no jsonl-spelunking agent can still correct
    the tool. Read-only: never writes config for you.
    """
    win = max(1, int(getattr(args, "days", 7) or 7))
    # threshold is on WORK (output+cache_creation), matching the widget's headline
    # metric — not total tokens, 97% of which is cache_read noise.
    thresh = int(getattr(args, "min_work", 50_000) or 50_000)
    data = build_data()
    days = data.get("days", [])[-win:]

    flagged = {}   # sid -> {"work":, "bucket":, "date":}
    for d in days:
        for s in d.get("sessions_list", []):
            if is_fallback(s.get("project", "")) and s.get("work", 0) >= thresh:
                cur = flagged.get(s["id"])
                if not cur or s["work"] > cur["work"]:
                    flagged[s["id"]] = {"work": s["work"], "bucket": s["project"],
                                        "date": (s.get("start") or "")[:10]}
    if not flagged:
        print(f"✓ 近 {win} 天分类健康：没有 ≥{thresh:,} work 的会话落进兜底桶。")
        return

    # re-scan just the flagged sessions for anchor + touched top-level dirs
    metas = {sid: {"anchor": None, "cwd": None} for sid in flagged}
    dircnt = {sid: defaultdict(int) for sid in flagged}
    for rec in scan.iter_records(scan.find_jsonl(PROJ_DIR, recursive=False)):
        sid = rec.get("sessionId")
        if sid not in flagged:
            continue
        mt = metas[sid]
        if rec.get("type") == "user" and rec.get("cwd"):
            mt["cwd"] = rec["cwd"]
            if mt["anchor"] is None and not _TMP_RX.search(rec["cwd"]):
                mt["anchor"] = rec["cwd"]
        elif rec.get("type") == "assistant":
            for c in ((rec.get("message") or {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
                    if not fp or _TMP_RX.search(fp):
                        continue
                    anc = mt["anchor"] or mt["cwd"]
                    if anc and fp.startswith(anc.rstrip("/") + "/"):
                        dircnt[sid][fp[len(anc.rstrip("/") + "/"):].split("/")[0]] += 1

    aliases = CFG.get("project_aliases", {})
    actionable, inert = [], 0
    for sid, info in flagged.items():
        anc = metas[sid]["anchor"]
        # a real project signal = a touched top-level dir that isn't a loose file
        # (has no ".") and isn't itself a fallback name. No signal → genuinely a
        # config-only / loose-file / idle session; leave it in its fallback bucket.
        segs = {k: v for k, v in dircnt[sid].items() if "." not in k and not is_fallback(k)}
        if not segs:
            inert += 1
            continue
        guess = max(segs.items(), key=lambda kv: kv[1])[0]
        actionable.append((sid, info, anc, aliases.get(guess, guess)))

    if not actionable:
        print(f"✓ 近 {win} 天没有可归类的漏网会话"
              f"（{inert} 个仅改配置 / 散文件 / 无文件活动的会话属正常兜底）。")
        return

    actionable.sort(key=lambda x: -x[1]["work"])
    print(f"⚠ 近 {win} 天有 {len(actionable)} 个会话疑似漏归类"
          f"（另有 {inert} 个仅改配置 / 散文件 / 无活动的会话属正常兜底，未列出）：\n")
    sugg_cwd, sugg_sess = {}, {}
    for sid, info, anc, guess in actionable:
        top = sorted(dircnt[sid].items(), key=lambda kv: -kv[1])[:5]
        print(f"  {sid[:8]}  {info['date']}  {info['work']:>10,} work  现归 [{info['bucket']}] → 建议 [{guess}]")
        print(f"      launch: {label_cwd(anc or '—')}   触及: {dict(top)}")
        # cwd_override only when the launch dir itself is the project dir (stable, reusable)
        if anc and os.path.basename(anc.rstrip("/")) == guess and anc not in CFG.get("cwd_overrides", {}):
            sugg_cwd[anc] = guess
        else:
            sugg_sess[sid] = guess
    # Emit ONE valid JSON blob (json.dumps handles commas/escaping) — printing raw
    # fragments with hand-rolled trailing commas would corrupt config.json on paste,
    # and load_config silently falls back to all-defaults on a parse error.
    snippet = {}
    if sugg_cwd:
        snippet["cwd_overrides"] = sugg_cwd
    if sugg_sess:
        snippet["session_overrides"] = sugg_sess
    print("\n── 把下面这些键合并进你的 config.json（已有同名块就并进去）──")
    print(json.dumps(snippet, ensure_ascii=False, indent=2))


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
    gp = sub.add_parser("glance", help="compact today's-usage JSON for the menubar widget")
    gp.add_argument("--days", type=int, default=1,
                    help="window size in days ending today (default: 1 = today)")
    dp = sub.add_parser("doctor", help="find mis-bucketed sessions + suggest config rules")
    dp.add_argument("--days", type=int, default=7,
                    help="window size in days ending today, capped at 30 (default: 7)")
    dp.add_argument("--min-work", type=int, default=50_000, dest="min_work",
                    help="ignore sessions below this work (output+cache_creation; default: 50000)")
    args = parser.parse_args()
    if args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "serve":
        cmd_serve(args)
    elif args.cmd == "glance":
        cmd_glance(args)
    elif args.cmd == "doctor":
        cmd_doctor(args)


if __name__ == "__main__":
    main()
