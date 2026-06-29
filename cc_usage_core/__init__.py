"""cc-usage-core · token-dashboard 与 cc-report 的共享内核 (Phase B).

- scan:   ~/.claude/projects 的文件发现 + jsonl 逐行解析 (两边逐字相同的样板)
- models: 模型注册表 (id → family / order / 定价) —— 定价的唯一真相, Phase C 各页 /api 也读它

刻意不收进来的: 各产品的调色板(珊瑚 vs 灰阶, 设计身份) 与 投影逻辑
(行为信号 / 日-session-项目时序), 以及对 "daily" 的定义 (UTC date vs 本地 TZ —— 两边本就不同)。
"""
