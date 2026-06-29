"""共享 jsonl 扫描层 —— 只做「文件发现 + 逐行 parse」, 不做任何投影。

token-dashboard 和 cc-report 都在 ~/.claude/projects 上各扫各的, 文件发现 + json
逐行解析这层是逐字相同的样板, 抽出来共用即可。时间/日期分桶、模型归类、各自的
聚合, 一律留在各产品自己的投影逻辑里 —— 两边对 "daily" 的定义不同(UTC date vs
本地 TZ), 故意不在这层统一, 否则会改掉它们现有的输出。
"""
import os
import glob
import json
from datetime import datetime, timezone


def find_jsonl(projects_dir, recursive=True):
    """发现 projects 目录下所有 session jsonl。

    recursive=True  → projects/**/*.jsonl  (token-dashboard 语义)
    recursive=False → projects/*/*.jsonl   (cc-report 语义: 只一层子目录, 不含直接落在 projects/ 下的文件)
    """
    if recursive:
        return glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)
    files = []
    for d in glob.glob(os.path.join(projects_dir, "*")):
        if os.path.isdir(d):
            files.extend(glob.glob(os.path.join(d, "*.jsonl")))
    return files


def iter_records(files):
    """逐行 yield 解析后的 dict 记录; 跳过空行 / 坏 JSON / 非 dict / 读不开的文件。

    产出的记录集合与两边原本各自的逐行循环逐字一致 (空行和坏行原本也都被 except 跳过)。
    """
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict):
                        yield rec
        except OSError:
            continue


def parse_ts(s):
    """ISO 时间戳 → aware datetime(UTC); 失败返回 None。(cc-report 原口径, 逐字搬运)"""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s)
    except Exception:
        return None
