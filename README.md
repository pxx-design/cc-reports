# cc-reports · Claude Code 日报 / 周报 / 月报

> 一个**完全本地**的 dashboard，扫你的 `~/.claude/projects/` jsonl，告诉你
> **今天 / 本周 / 本月**你用 Claude Code 做了什么、烧了多少 token、cache 命中率多少。
>
> **不联网 · 不上传 · 单脚本 + 单 HTML，零依赖**

![preview](preview.png)

## 它做什么

打开 Claude Code (cc) 时每条 prompt 都会写到 `~/.claude/projects/<cwd>/<sessionId>.jsonl`。
这个项目把那堆 jsonl 聚合成一个**点击可刷新**的 dashboard：

- **Hero stats**：sessions / prompts / output tokens / cache hit
- **时段活动**：日报 24 柱，按 model 堆叠（点柱子看模型分布）
- **近 7 日趋势**：tokens / sessions / prompts 三条 sparkline（点圆点看该日详情）
- **Token 用量构成**：output / cache_write / input 堆叠 bar + 一句话洞察
- **今日各项目用时**：自动识别你工作在哪个子项目（基于 Edit/Write 的 file_path）
- **今日 Sessions**：所有 session 的标题 + 时长 + tokens
- **周报 / 月报**：把日报升级成 day × 24h heatmap（点格子看该时段模型分布）

跨日的长 session（你不关 cc 窗口，连续用几天）会被自动**按日切片**，
所以每天的数字反映的是「**那天**做了什么」，不是「这个 session 起源在哪天」。

## 怎么用

### 路线 A · 让 Claude Code 自动跑（推荐）

```bash
git clone <this-repo>.git cc-reports
cd cc-reports
```

打开 Claude Code（在 `cc-reports` 目录），说一句：

```
读 ./SKILL.md，帮我生成 cc 报告
```

cc 会自动启 server 并打开 dashboard。

### 路线 B · 命令行

```bash
# 实时 dashboard（点击刷新会现场重算，cmd-r 看最新数据）
python3 cc-reports.py serve
# → cc-reports · http://localhost:8765/

# 一次性快照（生成 cc-reports-data.json，HTML 自动读）
python3 cc-reports.py build
```

`serve` 默认 8765 端口；占用就自动 +1 找空闲端口。

## 项目识别 / 配置

dashboard 会按下面 3 层优先级判断每个 session 算在哪个项目里：

```
1. cwd_overrides 配置（最优先）
   → 显式把某个 cwd 路径强制映射到项目名

2. cwd 是不是单项目根（含 **build manifest**：package.json / Cargo.toml /
   pyproject.toml / setup.py / go.mod / Gemfile / pom.xml / Makefile / 等）
   → 是：用 cwd 的最后一段目录名作为项目名
        （你在项目根目录里 `cc` 启动 → 项目名 = 这个目录名，符合直觉）

   注：`.git` 单独存在**不算**——很多 workspace / mega-repo 也用 git
   管理。必须有 build manifest 才算单项目根。

3. cwd 是多项目 workspace（没有上面那些 marker）
   → 看 session 内 Edit/Write 操作最多的第一级子目录作为项目名
   → 没文件操作 → "general"
```

**默认体验已经够好**——大多数人 `cd` 到项目根目录开 `cc`，第 2 步就能识别对。

### 想自定义就 copy config.example.json 改名 config.json：

```json
{
  "project_aliases": {
    "old-name": "01-new-name"
  },
  "root_file_projects": [
    ["^my-tool(-|\\.|$)", "my-tool"]
  ],
  "cwd_overrides": {
    "/Users/me/work/secret": "Acme Beta"
  }
}
```

| 字段 | 用途 |
|---|---|
| `project_aliases` | 改某个项目显示名（对 cwd basename 或第一级子目录都生效） |
| `root_file_projects` | workspace 模式时，cwd 根目录散文件按 regex 归到逻辑项目 |
| `cwd_overrides` | 自动识别错了？用绝对路径硬性指定项目名 |

编辑后浏览器点「刷新」即可生效，不用重启 server。

## 隐私

- **100% 本地**：脚本只读 `~/.claude/projects/*.jsonl` + 你 clone 的 HTML 模板
- **无网络调用**：server 只监听 `127.0.0.1`，不开放外部访问
- **数据不离开你的电脑**：除非你自己截图分享 / 把生成的 HTML 发出去
- 你的 `config.json`（含项目别名，可能透露内部代号）默认在 `.gitignore` 里，不会被 commit

## 文件结构

```
cc-reports/
├── README.md             你正在看
├── SKILL.md              cc 自动化协议
├── cc-reports.py         主脚本（build + serve）
├── cc-reports.html       dashboard 模板（数据通过 fetch 加载）
├── config.example.json   配置模板
├── .gitignore
└── LICENSE
```

运行后会生成：

```
├── config.json           ← 你的私有别名表（gitignore'd）
└── cc-reports-data.json  ← 静态快照（gitignore'd）
```

## 兼容性

- Python 3.10+
- macOS / Linux 验证过；Windows 应该 OK（用了 `os.path` 跨平台）
- 浏览器：Chrome / Safari / Edge / Firefox

## 相关

- **[cc-token-dashboard](https://github.com/...)**：同一作者的姊妹项目，做季度回顾 + 16 型 token 人格。
  cc-token-dashboard 是「年报 + 测一次玩」，cc-reports 是「每日工作日历」。互补不重叠。

## License

MIT
