---
name: cc-reports
description: 生成并打开 Claude Code 用量 dashboard——看自己今天/本周/本月用 cc 做了什么、发了多少 prompt、烧了多少 output token、cache 命中率多少、各项目分别花了多少时间。当用户说"看我的 cc 日报/周报/月报""我这周用 Claude Code 干了啥""我 cc 烧了多少 token""今天用 cc 做了什么""cc 用量/统计/报告"等时触发。100% 本地：只读 ~/.claude/projects 的 jsonl，不联网、不上传。
---

# cc-reports · 生成 Claude Code 工作日报

你的任务：**启动 `cc-reports.py serve`**，把 dashboard 开到用户浏览器里。

**重要**：全程本地处理，不联网，不上传任何数据。

---

## 数据源

- 必需：`~/.claude/projects/**/*.jsonl`（Claude Code 自动写）
- 主脚本：当前 repo 根目录的 `cc-reports.py`
- 模板：当前 repo 根目录的 `cc-reports.html`
- 可选：`./config.json`（用户的项目别名表，不存在也能跑）

如果 `~/.claude/projects/` 不存在或为空，提示用户：「你似乎还没用 Claude Code，跑几个 session 后再来。」

---

## 执行流程

### 1. 检查环境

```bash
python3 --version    # 需要 3.10+
ls ~/.claude/projects/ | head -1    # 至少一个 session 目录
```

如果 Python < 3.10，告诉用户先升级。

### 2. 启动 server

先定位 skill 资产目录（`cc-reports.py` / `cc-reports.html` / `cc_usage_core/` 与本 SKILL.md 同目录）：

```bash
# 个人 skill 安装路径；若不存在则兜底为当前目录（已 cd 到 clone 的 repo 根）
SKILL_DIR="$HOME/.claude/skills/cc-reports"
[ -f "$SKILL_DIR/cc-reports.py" ] || SKILL_DIR="$(pwd)"
python3 "$SKILL_DIR/cc-reports.py" serve
```

（CLI 用户也可以直接 `cd` 进 repo 根目录跑 `python3 cc-reports.py serve`。）

预期 stderr 输出：
```
cc-reports · http://localhost:8765/
  config:   loaded ./config.json | none (using defaults)
  tz:       <system tz>
  data:     http://localhost:8765/api/data (rebuilt on every request)
  Ctrl-C to stop
```

如果 8765 被占，脚本会自动 +1 找空闲端口（最多 8784）。**读 stderr 第一行抓真实 URL**。

### 3. 打开浏览器

```bash
open <url from stderr>      # macOS
xdg-open <url>              # Linux
start <url>                 # Windows
```

### 4. 后台运行

server 是阻塞前台进程。建议用 `run_in_background: true` 的方式启动，把 PID 报给用户，并告诉用户：

> Dashboard 已开在 <url>。停止用 `kill <PID>`，或在 terminal 按 Ctrl-C。

### 5. 检查项目归类质量（重要）

server 起来后，**主动**调一次 `curl -s http://localhost:<port>/api/data | jq '.days[-1].projects'`
看今天的项目识别结果。如果发现下面任一信号，**主动告知用户并询问要不要调整**——
不要等用户发现问题再被动响应：

| 异常信号 | 含义 |
|---|---|
| 项目名是 `src` / `lib` / `tests` / `components` / `app` / `pages` / `dist` / `build` 等典型项目**内部目录名** | 多半是用户在项目根目录启动 cc，但该项目没用 build manifest（dashboard 无法自动识别为单项目根）→ 走到 workspace 算法 → 把内部目录当项目名 |
| `root files` 或 `general` 占总用时 > 30% | 大量文件操作落到了兜底类目，识别精度差 |
| 项目数量 > 10（对个人用户而言） | 可能 workspace 拆分太碎，或用户在多个无关 cwd 工作 |
| 项目名和用户口中提到的项目对不上 | 用户的认知 ≠ 自动归类结果 |

发现异常时**对用户说**（参考措辞）：

> 我看到今天的项目识别里出现了 "src" / "lib" 这种像是项目内部目录的名字。
> 这通常是因为你在项目根目录里启动 cc，但项目里没有 `package.json` 这类
> build manifest 文件，所以 dashboard 没法自动识别成「单项目」。
> 要让我帮你修一下吗？

如果用户说要修，按下面流程：

1. 列出**该用户所有 session 的 cwd**：
   ```bash
   for f in ~/.claude/projects/*/*.jsonl; do
     jq -r 'select(.type=="user") | .cwd' "$f" 2>/dev/null | head -1
   done | sort -u
   ```
2. 把列表读给用户，**让用户告诉你每个 cwd 对应哪个项目名**（或者哪几个 cwd 应该合并到同一个项目）
3. 把答案写到 `config.json` 的 `cwd_overrides`：
   ```json
   {
     "cwd_overrides": {
       "/Users/foo/work/my-app": "MyApp",
       "/Users/foo/temp/exp1": "Experiment 1"
     }
   }
   ```
4. 让用户在浏览器点「刷新」按钮 → 现场重算 → 看新结果

如果 `config.json` 已存在，**先 read 再 patch**（保留用户已有的 project_aliases / root_file_projects）。

---

### 6. 项目名语义化（重要 · 跟 #5 同等重要）

v2 算法识别项目**归类**做对了，但有些"抽象兜底名"用户看不懂：

| 抽象名 | 来源 |
|---|---|
| `src` / `lib` / `tests` / `components` / `app` / `pages` / `dist` / `build` / `public` | 单项目根 cwd 无 build manifest，layer 3 把内部子目录当项目名 |
| `general` | 该 session 完全没文件操作（纯聊天/思考），归终极兜底 |
| `root files` | cwd 根目录的散文件归类 |
| `claude-config` | `.claude/` 目录硬编码归类 |

发现这些项目时，**主动**为用户做语义化命名（你是 Claude，有自然语言理解能力，给一个"人话"项目名比让用户自己手编 JSON 强太多）。

#### 协议

对每个抽象名项目：

**a. 读该项目的 session ai-titles**：
```bash
PORT=<server port from stderr>
PROJ="src"   # 替换为实际抽象名
curl -s "http://localhost:$PORT/api/data" \
  | jq -r --arg p "$PROJ" '[.days[].sessions_list[] | select(.project == $p)] | .[].title' \
  | sort -u | head -10
```

**b. 基于 ai-titles 给一个 2-6 字的人话项目名**：

阅读 titles 列表，理解这些 session 在做什么，给一个语义化的项目名。规则：
- 中文 workspace 倾向中文命名（贴近用户语言习惯）
- 英文项目目录倾向英文命名
- 名字要**具体到主题**，不要回 "general" 这种抽象（典型示例下面）
- 长度控制 2-6 字
- 不要带 emoji

**典型示例**：
```
'src' 下 sessions:
  - Build visual reference library
  - Implement search UI
  - Refactor state mgmt
→ 建议名：「Visual UI 实验室」 或 「UI 重构」

'claude-config' 下:
  - Configure feishu skill
  - Fix hook validation
  - Add MCP server
→ 建议名：「CC 配置调试」

'general' 下:
  - Explain how prompt caching works
  - Compare embedding models
→ 建议名：「咨询 & 探索」

'root files' 下:
  - Generate CCToken dashboard report
  - Create Vercel-style report manager
→ 建议名：「Dashboard 原型」
```

**c. 跟用户对话确认每个**：
```
你的「src」项目下有 3 个 session：
  • Build visual reference library
  • Implement search UI
  • Refactor state mgmt

建议改名为「Visual UI 实验室」。接受吗？想叫别的也告诉我。
```

允许用户：
- 接受建议名
- 给一个完全不同的名字
- 跳过（保留原名）

**d. 写入 `config.json` 的 `project_aliases`**：

读 `config.json`（不存在则 `cp config.example.json config.json`），patch `project_aliases` 字段，**保留所有其他字段**：

```bash
# 例：把 src → "Visual UI 实验室"
python3 -c "
import json, sys
p = 'config.json'
try: c = json.load(open(p))
except: c = {}
c.setdefault('project_aliases', {})
c['project_aliases']['src'] = 'Visual UI 实验室'
json.dump(c, open(p, 'w'), ensure_ascii=False, indent=2)
print('updated')
"
```

或直接用 jq / Edit tool 改 JSON。

**e. 告诉用户在浏览器点「刷新」按钮**，新名字立即生效（不需要重启 server）。

#### 边界

- `project_aliases` 是**全局** `name → name`。如果用户两个 workspace 都有 `src` 但意义不同，全局映射会冲突。
- 多数用户只有一个主要 workspace 不冲突。
- 冲突时引导用户用 `cwd_overrides`（按绝对 cwd 路径映射，更精细）。

---

## 项目识别如何工作（解释给用户听）

dashboard 把每个 session-day 切片归到一个项目，按下面 3 层优先级判断：

```
1. cwd_overrides 配置（最优先）
   → 用户在 config.json 显式把某个绝对 cwd 映射到项目名

2. cwd 是不是单项目根
   → 看 cwd 里是否有 build manifest:
     package.json / Cargo.toml / pyproject.toml / setup.py /
     go.mod / Gemfile / pom.xml / build.gradle / Makefile / 等
   → 是：用 cwd basename 作为项目名（你 cd 到项目根开 cc，符合直觉）
   → 注意：.git 单独存在不算（workspace 也常用 git）

3. cwd 是多项目 workspace（layer 2 失败时的回退）
   → 看 session 内 Edit/Write/Read 最多的第一级子目录
   → 没文件操作：归 "general"
```

跨多天的 session 在每天单独切片，每天可以归到不同项目（如果用户当天主要编辑了不同目录）。

---

## 如果用户问"它有什么用"

简短解释 dashboard 的核心信号：

- **Hero · Sessions**：今天打开了几个独立 cc 对话
- **Hero · Prompts**：你发了多少消息
- **Hero · Output**：cc 真实生成的 token（不含 cache_read，那是缓存命中读取）
- **Hero · Cache hit**：prompt 命中缓存的比例，越高越省钱（缓存命中价格只有 input 的 1/10）
- **Daily / Weekly / Monthly** tab 切换三种粒度
- 大多数图表上的元素都可点击，弹出详情

让用户自己点 hero stat 的 `?` 看每个指标的完整解释。

---

## 配置文件（可选）

`config.json` 完整字段（全部可选）：

```json
{
  "display_name": "",
  "cwd_overrides": {
    "/abs/path/to/cwd": "ProjectName"
  },
  "project_aliases": {
    "old-folder-name": "NewProjectName"
  },
  "root_file_projects": [
    ["^my-tool(-|\\.|$)", "my-tool"]
  ]
}
```

| 字段 | 何时用 |
|---|---|
| `display_name` | 自定义顶栏显示名；留空则自动回退：git user.name → 系统全名 → 登录名 |
| `cwd_overrides` | 自动识别错了 / 想给项目改名（最常用，强烈推荐先用这个） |
| `project_aliases` | 多个旧目录名想合并到一个规范名（如老路径 "app" → "01-app"） |
| `root_file_projects` | workspace 模式下，cwd 根目录的散文件归到逻辑项目 |

编辑后浏览器里点「刷新」按钮即可生效（不需要重启 server）。

---

## 故障排查

| 症状 | 检查 |
|---|---|
| `数据加载失败` | server 没启动，或被 firewall 拦了 localhost。重启 `python3 cc-reports.py serve` |
| Hero 显示 0 sessions | `~/.claude/projects/` 没数据。先用 cc 跑几个 session |
| 项目名不对 | 编辑 `config.json` 加别名 |
| PDF 导出颜色丢 | 用户的浏览器把"背景图形"关了。在打印对话框「更多设置」勾上 |
| port 8765 占用 | 脚本会自动找下一个空闲端口，看 stderr 第一行 |

---

## 不要做的事

- ❌ 不要修改用户的 `~/.claude/` 任何文件，**只读**
- ❌ 不要把 jsonl / 别名 / 项目名上传到任何远程服务
- ❌ 不要自动生成 `config.json`，让用户自己 copy `config.example.json` 决定要不要
- ❌ 不要 build 静态 HTML 当成最终产物——live server 模式才能体现"点击刷新"价值
