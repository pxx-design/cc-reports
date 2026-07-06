import AppKit
import WebKit
import Foundation

// ── cc-glance v2 · 常驻桌面浮窗(项目维度可视化)─────────────────
// 菜单栏 ⚡ 图标显示今日产出;点击开/关一个置顶迷你仪表盘(NSPanel + WKWebView)。
// 数据来自 `cc-reports.py glance`(纯本地扫 ~/.claude/projects,不联网),
// Swift 每次刷新把 JSON 注入 WebView,HTML 负责画项目条 / sparkline / 动画。

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

// ── 跑 glance,拿原始 JSON 字符串(直接注入 WebView,不在 Swift 解析)──
func runGlanceRaw() -> String? {
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
          let s = String(data: data, encoding: .utf8),
          !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    else { return nil }
    return s
}

// 菜单栏标题只需 tokens.work 一个数
func workFromJSON(_ s: String) -> Int? {
    guard let d = s.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
          let tk = obj["tokens"] as? [String: Any],
          let w = tk["work"] as? NSNumber else { return nil }
    return w.intValue
}

func fmtTok(_ n: Int) -> String {
    if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
    if n >= 1_000 { return String(format: "%.0fK", Double(n) / 1_000) }
    return "\(n)"
}

// ── App ─────────────────────────────────────────────────────
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var item: NSStatusItem!
    var panel: NSPanel!
    var web: WKWebView!
    var timer: Timer?
    var lastJSON: String?
    var webLoaded = false
    var refreshing = false

    func applicationDidFinishLaunching(_ n: Notification) {
        setupStatusItem()
        setupPanel()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        if ProcessInfo.processInfo.environment["CCG_AUTOSHOW"] != nil {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.togglePanel() }
        }
    }

    // ── 菜单栏图标(左键开关浮窗 / 右键小菜单)──
    func setupStatusItem() {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        item.button?.title = "⚡ …"
        item.button?.target = self
        item.button?.action = #selector(statusClick)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
    }

    @objc func statusClick() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            showContextMenu()
        } else {
            togglePanel()
        }
    }

    func showContextMenu() {
        let m = NSMenu()
        let mk: (String, Selector, String) -> Void = { title, sel, key in
            let mi = NSMenuItem(title: title, action: sel, keyEquivalent: key)
            mi.target = self
            m.addItem(mi)
        }
        mk("刷新", #selector(doRefresh), "")
        mk("打开完整报告 ↗", #selector(openFull), "")
        m.addItem(.separator())
        mk("退出", #selector(quit), "q")
        item.menu = m
        item.button?.performClick(nil)   // 弹出菜单
        item.menu = nil                   // 复位,下次左键回到"开关浮窗"
    }

    // ── 浮窗 ──
    func setupPanel() {
        let size = NSSize(width: 520, height: 360)
        panel = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.level = .floating              // 置顶
        panel.isFloatingPanel = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.isMovableByWindowBackground = true   // 拖背景移动
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "ccg")
        web = WKWebView(frame: NSRect(origin: .zero, size: size), configuration: cfg)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")   // 透明底,露出 CSS 圆角卡
        web.wantsLayer = true
        web.layer?.cornerRadius = 24
        web.layer?.masksToBounds = true
        if let url = Bundle.module.url(forResource: "glance", withExtension: "html") {
            web.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
        }
        panel.contentView = web
    }

    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) {
        webLoaded = true
        injectIfReady()
    }

    func injectIfReady() {
        guard webLoaded, let json = lastJSON else { return }
        web.evaluateJavaScript("window.__setData(\(json))")
    }

    func togglePanel() {
        if panel.isVisible {
            saveFrame()
            panel.orderOut(nil)
        } else {
            positionPanel()
            panel.orderFrontRegardless()
            injectIfReady()
            refresh()   // 打开即拉最新
        }
    }

    // 位置:优先上次保存;否则贴到菜单栏图标下方
    func positionPanel() {
        if let s = UserDefaults.standard.string(forKey: "ccg.origin") {
            let p = NSPointFromString(s)
            if NSScreen.screens.contains(where: { $0.frame.contains(p) }) {
                panel.setFrameOrigin(p); return
            }
        }
        if let bwin = item.button?.window {
            let bf = bwin.frame
            let x = bf.midX - panel.frame.width / 2
            let y = bf.minY - panel.frame.height - 6
            panel.setFrameOrigin(NSPoint(x: x, y: y))
        } else {
            panel.center()
        }
    }

    func saveFrame() {
        UserDefaults.standard.set(NSStringFromPoint(panel.frame.origin), forKey: "ccg.origin")
    }

    // ── 数据刷新 ──
    @objc func doRefresh() { refresh() }

    func refresh() {
        if refreshing { return }
        refreshing = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let json = runGlanceRaw()
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.refreshing = false
                if let json = json {
                    self.lastJSON = json
                    if let w = workFromJSON(json) { self.item.button?.title = "⚡\(fmtTok(w))" }
                    self.injectIfReady()
                } else {
                    self.item.button?.title = "⚡ –"
                }
            }
        }
    }

    // ── WebView 按钮回传(刷新 / 打开完整报告)──
    func userContentController(_ u: WKUserContentController, didReceive msg: WKScriptMessage) {
        switch msg.body as? String {
        case "refresh": refresh()
        case "openFull": openFull()
        default: break
        }
    }

    @objc func quit() { NSApp.terminate(nil) }

    // 打开完整 dashboard:复用已跑的 cc-reports(靠 /api/data 签名认准),否则冷启
    @objc func openFull() {
        let py = scriptPath()
        let sh = """
        for p in $(seq 8765 8784); do
          if curl -s --max-time 3 "http://localhost:$p/api/data" | grep -q '"generated_at"'; then
            open "http://localhost:$p/"; exit 0
          fi
        done
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
        try? t.run()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // 无 dock 图标,纯菜单栏 + 浮窗
let delegate = AppDelegate()
app.delegate = delegate
app.run()
