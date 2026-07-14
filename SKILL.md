---
name: cc-reports
description: Claude Code 用量报告——看自己今天/本周/本月用 cc 做了什么：每个项目花了多少时间、烧了多少 token、等效多少 API 成本、几点在干活。当用户说"看我的 cc 日报/周报/月报""我这周用 Claude Code 干了啥""我 cc 烧了多少 token""今天用 cc 做了什么""某个项目我花了多久""cc 用量/统计/报告"，或抱怨项目归类不对时触发。100% 本地：只读 ~/.claude/projects 的 jsonl，不联网、不上传。
---

# cc-reports · Claude Code 工作日报

你能做三件事，按用户的问法选：

| 用户在问 | 你做什么 |
|---|---|
| "打开我的日报 / 周报 / 月报" | **起 dashboard**（§2），开到浏览器里 |
| "我这周在 X 上花了多久""昨天烧了多少 token" | **直接答**（§3）——读 JSON 回答，不必开网页 |
| "为什么这些 token 算到 root files 了""项目名不对" | **跑 doctor 修分类**（§4） |

**红线**：全程本地，只读 `~/.claude/projects/**/*.jsonl`，不联网、不上传、不改用户的 `~/.claude/` 任何文件。

---

## 1. 定位与环境

```bash
# 装成 skill(软链)时资产在这;否则就是当前 clone 的 repo 根
CC="$HOME/.claude/skills/cc-reports/cc-reports.py"
[ -f "$CC" ] || CC="$(pwd)/cc-reports.py"
# 用 Homebrew 装过的话,PATH 里直接有 cc-reports 命令,可代替 "python3 $CC"
```

需要 python3（实测 3.9 起可用，纯标准库无依赖）。若 `~/.claude/projects/` 不存在或为空，告诉用户：「你似乎还没用过 Claude Code，跑几个 session 再来。」

---

## 2. 起 dashboard

```bash
python3 "$CC" serve        # 或:cc-reports serve
```

- **后台跑**（`run_in_background: true`），server 是阻塞进程。
- **读输出第一行拿真实 URL**（`cc-reports · http://localhost:PORT/`）——8765 被占时脚本自动 +1 往后找（最多 8784），所以**别假设是 8765**。
- 拿到 URL 后 `open <url>`（macOS）/ `xdg-open`（Linux）/ `start`（Windows），并把停止方式告诉用户（`kill <PID>` 或 Ctrl-C）。
- 页面上的数字大多可点：柱子出该小时的模型分布、`≈ $xx` 出按模型的成本明细、项目卡可展开。

页面有什么（用户问起时照实说，别编）：

- **四个里程碑**：最长专注 / 投入时长（各 session 活跃时长之和，标出占今天跨度多少）/ 今日穿梭（碰了几个项目、切换几次）/ 连续开工
- **时段 token 消耗**：24 根柱子按模型堆叠；背后斜纹 = 近 7 日同时段的常态，今天高出还是塌下去一眼可读。右侧解析时段分布、峰值、今天是日均的几倍
- **各项目用量**：时间占比条 + 项目卡（主指标是**活跃时长**，不是 token）
- **等效 API 成本**：按官方价目表折算，**不是订阅实付**，永远带 `≈`
- 周/月报：天 × 24h 热力图 + 作息节奏解析

---

## 3. 直接回答用量问题（不开网页）

用户只想知道一个数时，别甩他一个网页。拿 JSON 自己读：

```bash
python3 "$CC" glance                     # 今天:项目/时段/token/活跃时长(精简)
python3 "$CC" build && cat cc-reports-data.json   # 近 30 天全量(含每日 sessions_list)
```

口径必须说对，**这是这个产品的诚实底线**：

- **产出 = output + cache_creation**。token **总量**里约 90% 是 `cache_read`（每轮重读上下文，随会话变长 N² 膨胀）——拿总量当"干了多少活"会严重误导：一个开着不关的长会话看着比谁都高产。
- **时间才是主轴**：项目排序按活跃时长（时间＝心思落在哪），token 是副指标。
- `$` 是**等效 API 成本**（按价目表折算），订阅制下拿不到真实账单，别说成"你花了多少钱"。

---

## 4. 分类不准时:跑 doctor,别手搓

归类按「session 启动锚点 + 绝对路径」解析，`/tmp`、scratchpad 不计票，兜底桶（`root files` / `general` / `claude-config`）不参与竞选、只在真项目零票时垫底。零配置下兜底率通常 <10%。

出问题时**用 doctor，不要自己写 jq 去扫 jsonl**：

```bash
python3 "$CC" doctor                 # 近 7 天,列出落进兜底桶的大额会话
python3 "$CC" doctor --days 30 --min-work 500000
```

它会指出哪些会话疑似漏归类、猜出真实项目，并**直接吐出可粘进 `config.json` 的规则行**。你的活是：

1. 把 doctor 的判断读给用户，确认哪些猜对了
2. 帮他把规则写进 `config.json`（**先 read 再 patch**，别覆盖已有字段；不存在就从 `config.example.json` 拷）
3. 让他在浏览器点「刷新」——现场重算，不用重启 server

`config.json` 字段（全部可选）：

| 字段 | 何时用 |
|---|---|
| `cwd_overrides` | 把某个绝对 cwd 映射成项目名（最常用，优先推这个） |
| `session_overrides` | 把某个 sessionId 钉死成项目名（应付跨天在多项目间跳的会话） |
| `project_aliases` | 多个旧目录名合并到一个规范名（全局 name→name，两个 workspace 同名会冲突，冲突时改用 cwd_overrides） |
| `root_file_projects` | workspace 模式下，cwd 根目录的散文件归到逻辑项目 |
| `display_name` | 顶栏显示名；留空自动回退 git user.name → 系统全名 → 登录名 |

抽象名（`src` / `lib` / `components` 这类项目**内部**目录名冒充项目名）多半是：用户在项目根开的 cc，但项目里没有 `package.json` 这类 build manifest，于是走了 workspace 算法。这种情况主动提出帮他配 `cwd_overrides`，并基于 session 标题给一个人话项目名让他确认。

---

## 5. 桌面浮窗 cc-glance(用户问起时指路,不要自动装)

同一个内核还有一个常驻桌面的浮窗（macOS，Swift，CRT 终端造型，顶沿坐着个跟你 token 流作息的像素小人）。**它不归你管**：日常唤起靠菜单栏图标或 `⌥⇧R` 全局热键，全程不经过 Claude。

用户问「怎么装浮窗」时给路，别替他执行——装一个开机自启的常驻 app + 抢全局热键，是显式的人类动作：

```bash
brew tap pxx-design/cc-reports https://github.com/pxx-design/cc-reports
brew install cc-glance
cc-glance                          # 菜单栏图标 = 进程本身,进程活着才有
brew services start cc-glance      # 想开机自启再跑这个
```

---

## 故障排查

| 症状 | 检查 |
|---|---|
| 页面「数据加载失败」 | server 没起来，或被防火墙拦了 localhost |
| 0 sessions | `~/.claude/projects/` 没数据，先用 cc 跑几个 session |
| 项目名不对 | §4 跑 doctor |
| 8765 被占 | 脚本自动往后找端口，看 stderr 第一行 |
| PDF 导出没颜色 | 浏览器打印对话框里勾上「背景图形」 |

## 不要做的事

- ❌ 不改用户 `~/.claude/` 下任何文件（**只读**）
- ❌ 不把 jsonl / 项目名 / 任何数据发到远程
- ❌ 不擅自生成 `config.json`——让用户决定要不要
- ❌ 不用 `build` 出的静态 HTML 当最终交付（`serve` 才有「点刷新现场重算」）
- ❌ 不替用户安装浮窗（§5）
