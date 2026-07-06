import AppKit
import Foundation

// ── cc-glance · 常驻菜单栏用量浮层 ──────────────────────────────
// 菜单栏常显今天用量(total tokens),点开下拉看项目明细。
// 数据来自 `cc-reports.py glance`(纯本地扫 ~/.claude/projects,不联网)。
// 每次打开菜单 / 点刷新 / 每 5 分钟自动跑一次。

// ── 定位 cc-reports.py(优先个人 skill 目录,回退到 repo)──────
func scriptPath() -> String {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    let candidates = [
        home + "/.claude/skills/cc-reports/cc-reports.py",
        home + "/Desktop/cursor1/15-cc-reports/cc-reports.py",
    ]
    for p in candidates where FileManager.default.fileExists(atPath: p) { return p }
    return candidates[0]
}

// ── 数据模型 ────────────────────────────────────────────────
struct Proj {
    let name: String; let tokens: Int; let output: Int; let cacheCreation: Int
    let activeMin: Int; let fallback: Bool
    var work: Int { output + cacheCreation }   // "真实产出":生成 + 写缓存,排除缓存重读
}
struct Glance {
    let total: Int, output: Int, cacheCreation: Int, activeMin: Int, sessions: Int
    let start: String
    let projects: [Proj]
    var work: Int { output + cacheCreation }   // 跟 dashboard「Output」卡同口径
}

// ── 跑 glance,解析 JSON ─────────────────────────────────────
func runGlance() -> Glance? {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    task.arguments = ["python3", scriptPath(), "glance"]
    let out = Pipe()
    task.standardOutput = out
    task.standardError = Pipe()
    do { try task.run() } catch { return nil }
    let data = out.fileHandleForReading.readDataToEndOfFile()
    task.waitUntilExit()
    guard task.terminationStatus == 0,
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return nil }

    let tk = obj["tokens"] as? [String: Any] ?? [:]
    let rng = obj["range"] as? [String: Any] ?? [:]
    let projs = (obj["projects"] as? [[String: Any]] ?? []).map { p in
        Proj(name: p["name"] as? String ?? "?",
             tokens: (p["tokens"] as? NSNumber)?.intValue ?? 0,
             output: (p["output"] as? NSNumber)?.intValue ?? 0,
             cacheCreation: (p["cache_creation"] as? NSNumber)?.intValue ?? 0,
             activeMin: (p["active_min"] as? NSNumber)?.intValue ?? 0,
             fallback: (p["fallback"] as? Bool) ?? false)
    }
    return Glance(
        total: (tk["total"] as? NSNumber)?.intValue ?? 0,
        output: (tk["output"] as? NSNumber)?.intValue ?? 0,
        cacheCreation: (tk["cache_creation"] as? NSNumber)?.intValue ?? 0,
        activeMin: (obj["active_min"] as? NSNumber)?.intValue ?? 0,
        sessions: (obj["sessions"] as? NSNumber)?.intValue ?? 0,
        start: rng["start"] as? String ?? "",
        projects: projs)
}

// ── 格式化 ──────────────────────────────────────────────────
func fmtTok(_ n: Int) -> String {
    if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
    if n >= 1_000 { return String(format: "%.0fK", Double(n) / 1_000) }
    return "\(n)"
}
func fmtMin(_ m: Int) -> String {
    if m >= 60 { return "\(m / 60)h\(m % 60)m" }
    return "\(m)m"
}

// ── App ─────────────────────────────────────────────────────
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    let menu = NSMenu()
    var timer: Timer?
    var cache: Glance?          // 上一次成功的数据快照
    var refreshing = false

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        item.button?.title = "⚡ …"
        menu.delegate = self
        item.menu = menu
        rebuildMenu(nil)        // 先占位,数据后台拉
        refreshAsync()
        // 每 5 分钟被动刷一次菜单栏数字
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.refreshAsync()
        }
    }

    // 打开菜单前:用缓存瞬间重建(不阻塞),同时后台拉最新
    func menuNeedsUpdate(_ menu: NSMenu) {
        rebuildMenu(cache)
        refreshAsync()
    }

    // 后台跑 glance,回主线程更新菜单栏数字 + 菜单(菜单开着也会活更新)
    func refreshAsync() {
        if refreshing { return }
        refreshing = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let g = runGlance()
            DispatchQueue.main.async {
                self.refreshing = false
                if let g = g { self.cache = g }
                self.item.button?.title = self.cache.map { "⚡\(fmtTok($0.work))" } ?? "⚡ –"
                self.rebuildMenu(self.cache)
            }
        }
    }

    func rebuildMenu(_ g: Glance?) {
        menu.removeAllItems()
        guard let g = g else {
            menu.addItem(dim(refreshing ? "加载中…" : "数据读取失败"))
            addFooter()
            return
        }

        // 头部:日期 + total + output + 活跃 + session 数
        let head = NSMenuItem(title: "今天 \(g.start.suffix(5))", action: nil, keyEquivalent: "")
        head.attributedTitle = NSAttributedString(
            string: "今天 \(g.start.suffix(5))",
            attributes: [.font: NSFont.boldSystemFont(ofSize: 12)])
        menu.addItem(head)
        menu.addItem(dim("产出 \(fmtTok(g.work)) · 活跃 \(fmtMin(g.activeMin)) · \(g.sessions) session"))
        menu.addItem(dim("含缓存重读共 \(fmtTok(g.total)) tokens"))
        menu.addItem(.separator())

        // 项目行:真项目在前(sort 已把 fallback 沉底)
        if g.projects.isEmpty {
            menu.addItem(dim("今天还没用量"))
        }
        for p in g.projects {
            let name = p.fallback ? p.name : p.name
            let row = "\(name)    \(fmtMin(p.activeMin)) · \(fmtTok(p.work))"
            let mi = NSMenuItem(title: row, action: nil, keyEquivalent: "")
            let color: NSColor = p.fallback ? .tertiaryLabelColor : .labelColor
            mi.attributedTitle = NSAttributedString(
                string: row,
                attributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular),
                             .foregroundColor: color])
            menu.addItem(mi)
        }
        addFooter()
    }

    func addFooter() {
        menu.addItem(.separator())
        add("🔄 刷新", #selector(doRefresh))
        add("打开完整报告 ↗", #selector(openFull))
        menu.addItem(.separator())
        add("退出", #selector(quit))
    }

    // helpers
    func dim(_ s: String) -> NSMenuItem {
        let mi = NSMenuItem(title: s, action: nil, keyEquivalent: "")
        mi.attributedTitle = NSAttributedString(
            string: s, attributes: [.font: NSFont.systemFont(ofSize: 11),
                                    .foregroundColor: NSColor.secondaryLabelColor])
        return mi
    }
    func add(_ title: String, _ sel: Selector) {
        let mi = NSMenuItem(title: title, action: sel, keyEquivalent: "")
        mi.target = self
        menu.addItem(mi)
    }

    @objc func doRefresh() { refreshAsync() }
    @objc func quit() { NSApp.terminate(nil) }

    // 打开完整 dashboard:后台起 serve,读首行 URL,open 之
    @objc func openFull() {
        item.button?.title = "启动中…"      // 立即反馈,冷启那几秒不至于"像没反应"
        let py = scriptPath()
        let sh = """
        # 1) 复用已在跑的 cc-reports(靠 /api/data 的 "generated_at" 签名认准自己,
        #    避免抓到端口段里别的本地 server,如灵感库)
        for p in $(seq 8765 8784); do
          if curl -s --max-time 3 "http://localhost:$p/api/data" | grep -q '"generated_at"'; then
            open "http://localhost:$p/"; exit 0
          fi
        done
        # 2) 没有则新起,等最多 ~15s 抓 URL,nohup 让它活过本脚本
        f=$(mktemp)
        nohup /usr/bin/env python3 "\(py)" serve >"$f" 2>&1 &
        for i in $(seq 1 60); do
          u=$(grep -o 'http://localhost:[0-9]*/' "$f" | head -1)
          [ -n "$u" ] && { open "$u"; exit 0; }
          sleep 0.25
        done
        """
        let t = Process()
        t.executableURL = URL(fileURLWithPath: "/bin/bash")
        t.arguments = ["-c", sh]
        t.terminationHandler = { [weak self] _ in      // 开完(或超时)恢复数字
            DispatchQueue.main.async { self?.refreshAsync() }
        }
        do { try t.run() } catch { refreshAsync() }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // 无 dock 图标,纯菜单栏常驻
let delegate = AppDelegate()
app.delegate = delegate
app.run()
