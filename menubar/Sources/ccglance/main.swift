import AppKit
import WebKit
import Foundation
import Carbon.HIToolbox

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

// ── 菜单栏图标:迷你 MOD-CC 设备(机身 + 电平条 + LED),模板图自适应亮暗 ──
func statusIcon() -> NSImage {
    let img = NSImage(size: NSSize(width: 18, height: 14), flipped: false) { _ in
        NSColor.black.set()
        // 机身
        let body = NSBezierPath(roundedRect: NSRect(x: 0.75, y: 0.75, width: 16.5, height: 12.5),
                                xRadius: 3.2, yRadius: 3.2)
        body.lineWidth = 1.5
        body.stroke()
        // 三条递减电平条(项目用量的缩影)
        let bars: [(y: CGFloat, w: CGFloat)] = [(9.1, 8.5), (6.0, 6.0), (2.9, 3.5)]
        for b in bars {
            NSBezierPath(roundedRect: NSRect(x: 3.2, y: b.y, width: b.w, height: 1.9),
                         xRadius: 0.95, yRadius: 0.95).fill()
        }
        // LED(右上)
        NSBezierPath(ovalIn: NSRect(x: 13.1, y: 9.2, width: 2.1, height: 2.1)).fill()
        return true
    }
    img.isTemplate = true   // 系统按菜单栏亮暗自动着色
    return img
}

// ── 拖动把手:盖在 WebView 上的原生条,mouseDown 即拖窗 ──
// (WKWebView 不认 -webkit-app-region,必须原生接管)
class DragBar: NSView {
    override func mouseDown(with event: NSEvent) {
        window?.performDrag(with: event)
    }
    // 丝印带右键 → 原生菜单(低频动作:刷新/隐藏/退出)
    override func rightMouseDown(with event: NSEvent) {
        guard let d = globalDelegate else { return }
        let m = NSMenu()
        let mk: (String, Selector) -> Void = { title, sel in
            let mi = NSMenuItem(title: title, action: sel, keyEquivalent: "")
            mi.target = d
            m.addItem(mi)
        }
        mk("刷新数据", #selector(AppDelegate.doRefresh))
        mk("隐藏浮窗(⌥⇧R 唤回)", #selector(AppDelegate.hidePanel))
        m.addItem(.separator())
        mk("退出 cc-glance", #selector(AppDelegate.quit))
        NSMenu.popUpContextMenu(m, with: event, for: self)
    }
}

// ── 桌宠拖拽层:盖住顶部小人区(212×88 设计px),按住即拖窗,双击转发 JS 续杯 ──
final class PetBar: DragBar {
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            globalDelegate?.web.evaluateJavaScript("window.__pet&&window.__pet.refill&&window.__pet.refill()")
            return
        }
        super.mouseDown(with: event)   // DragBar: performDrag
    }
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
        registerHotkey()   // ⌥⇧R 全局开关(菜单栏图标可能被刘海吞,热键保底)
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        // 桌宠动画在顶部透明区移动(爱心/起跳),透明窗口的系统投影缓存不跟着刷,
        // 会留灰色半透明鬼影——定期重算投影兜底
        Timer.scheduledTimer(withTimeInterval: 0.8, repeats: true) { [weak self] _ in
            guard let self = self, self.panel.isVisible else { return }
            self.panel.invalidateShadow()
        }
        // 常驻浮窗定位:启动即显示(⌥⇧R / 点图标可收起)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            if self?.panel.isVisible == false { self?.togglePanel() }
        }
    }

    // ── 全局热键 ⌥⇧R(Carbon,无需辅助功能权限)──
    func registerHotkey() {
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ -> OSStatus in
            DispatchQueue.main.async { globalDelegate?.togglePanel() }
            return noErr
        }, 1, &eventType, nil, nil)
        var ref: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: OSType(0x63636731), id: 1)
        RegisterEventHotKey(UInt32(kVK_ANSI_R), UInt32(optionKey | shiftKey),
                            hotKeyID, GetApplicationEventTarget(), 0, &ref)
    }

    // ── 菜单栏图标(左键开关浮窗 / 右键小菜单)──
    func setupStatusItem() {
        // 新状态项默认排最左 → 刘海机型上会被吞掉。预写偏好位置(距屏幕右缘 pt)
        // 再挂 autosave 名,系统按它摆放,落在右侧可见区。
        let posKey = "NSStatusItem Preferred Position ccg"
        if UserDefaults.standard.object(forKey: posKey) == nil {
            UserDefaults.standard.set(240.0, forKey: posKey)
        }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.autosaveName = "ccg"
        item.button?.image = statusIcon()
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
    // 设计稿 520×360,整体缩至 2/3(shona: 屏上原尺寸偏大)
    // +80 设计高:顶部桌宠区(小人坐在框顶沿,HTML .device margin-top:80px 对应)
    let zoom: CGFloat = 2.0 / 3.0
    let petStrip: CGFloat = 80
    let store = UserDefaults(suiteName: "com.shona.ccglance")!
    var folded: Bool { store.bool(forKey: "ccg.folded") }
    // 收起 = 按键列(96)+列间距(15)收进去,只剩屏幕
    var panelWidth: CGFloat { (folded ? 520 - 96 - 15 : 520) * zoom }

    func setupPanel() {
        let zoom = self.zoom
        let size = NSSize(width: panelWidth, height: (360 + petStrip) * zoom)
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
        web.pageZoom = zoom                              // HTML 按 520×360 设计,整窗缩放
        web.wantsLayer = true
        web.layer?.cornerRadius = 24 * zoom
        web.layer?.masksToBounds = true
        if let url = Bundle.module.url(forResource: "glance", withExtension: "html") {
            web.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
        }

        // 容器:WebView + 上下拖动把手(机身丝印带,各 19pt)
        let container = NSView(frame: NSRect(origin: .zero, size: size))
        web.autoresizingMask = [.width, .height]
        container.addSubview(web)
        let barH = 19 * zoom
        // 顶部拖拽条下移到机身丝印带(桌宠区在其上,留给小人点击)
        let topBar = DragBar(frame: NSRect(x: 0, y: size.height - petStrip * zoom - barH,
                                           width: size.width - 52 * zoom, height: barH))  // 右侧留给 LED 点击
        topBar.autoresizingMask = [.width, .minYMargin]
        let bottomBar = DragBar(frame: NSRect(x: 0, y: 0, width: size.width, height: barH))
        bottomBar.autoresizingMask = [.width, .maxYMargin]
        // 桌宠带=拖动热区(shona 点名):按住小人拖窗,双击续杯
        let petBar = PetBar(frame: NSRect(x: 0, y: size.height - 88 * zoom,
                                          width: 212 * zoom, height: 88 * zoom))
        petBar.autoresizingMask = [.minYMargin]
        container.addSubview(topBar)
        container.addSubview(bottomBar)
        container.addSubview(petBar)
        panel.contentView = container

        // 拖完即记住位置(下次开窗回到原位)
        NotificationCenter.default.addObserver(
            forName: NSWindow.didMoveNotification, object: panel, queue: .main
        ) { [weak self] _ in self?.saveFrame() }
    }

    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) {
        webLoaded = true
        if folded { web.evaluateJavaScript("window.__setFold(true)") }
        injectIfReady()
    }

    // 折叠/展开:同步收放窗宽(右缘不动,屏幕稳在原位),记住状态
    func setFolded(_ f: Bool) {
        store.set(f, forKey: "ccg.folded")
        var fr = panel.frame
        let newW = panelWidth
        fr.origin.x += fr.width - newW   // 保持右缘位置
        fr.size.width = newW
        panel.setFrame(fr, display: true, animate: true)
        saveFrame()
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

    // 位置:优先上次保存;否则开在主屏(排列设置里带菜单栏那块)右上角
    func positionPanel() {
        if let s = store.string(forKey: "ccg.origin") {
            let p = NSPointFromString(s)
            if NSScreen.screens.contains(where: { $0.frame.contains(p) }) {
                panel.setFrameOrigin(p); return
            }
        }
        if let scr = NSScreen.screens.first {
            let v = scr.visibleFrame
            let x = v.maxX - panel.frame.width - 16
            let y = v.maxY - panel.frame.height - 10
            panel.setFrameOrigin(NSPoint(x: x, y: y))
        } else {
            panel.center()
        }
    }

    func saveFrame() {
        store.set(NSStringFromPoint(panel.frame.origin), forKey: "ccg.origin")
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
                    // 标题保持纯 ⚡(窄,不被菜单栏挤掉);数字放悬停提示
                    if let w = workFromJSON(json) { self.item.button?.toolTip = "今日产出 \(fmtTok(w)) · 点击开关浮窗" }
                    self.injectIfReady()
                } else {
                    self.item.button?.toolTip = "数据读取失败"
                }
            }
        }
    }

    // ── WebView 按钮回传(刷新 / 打开完整报告)──
    func userContentController(_ u: WKUserContentController, didReceive msg: WKScriptMessage) {
        switch msg.body as? String {
        case "refresh": refresh()
        case "openFull": openFull()
        case "fold": setFolded(true)
        case "unfold": setFolded(false)
        case "hide": hidePanel()
        default: break
        }
    }

    @objc func hidePanel() {
        saveFrame()
        panel.orderOut(nil)
    }

    @objc func quit() {
        // launchd KeepAlive 会拉活普通退出——先卸载 agent 再终止,退得干净
        let t = Process()
        t.executableURL = URL(fileURLWithPath: "/bin/bash")
        t.arguments = ["-c", "launchctl unload \"$HOME/Library/LaunchAgents/com.shona.ccglance.plist\" 2>/dev/null &"]
        try? t.run()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { NSApp.terminate(nil) }
    }

    // 打开完整 dashboard:复用已跑的 cc-reports,否则冷启。探活抓 / 页的 title 特征串认准
    // 是自己(避免误开别的本地 server),而非抓 stdout——老写法 grep nohup serve 的 stdout,
    // 但 print 块缓冲 + serve_forever 永不返回,URL 永远刷不到文件、首点必哑。用 / 而非
    // /api/data 探活:/ 从磁盘秒回,/api/data 每次带 utilization 重建要 ~3s、短超时必落空。
    // 后台线程跑、waitUntilExit 拿真实结果,回传 __openDone(ok) 收尾按钮 loading。
    @objc func openFull() {
        let py = scriptPath()
        let sh = """
        probe() {
          for p in $(seq 8765 8784); do
            curl -s --max-time 3 "http://localhost:$p/" | grep -q "Claude Code journal" && { echo "$p"; return 0; }
          done
          return 1
        }
        if p=$(probe); then open "http://localhost:$p/"; exit 0; fi
        nohup /usr/bin/env python3 "\(py)" serve >/dev/null 2>&1 &
        for i in $(seq 1 40); do
          sleep 0.25
          if p=$(probe); then open "http://localhost:$p/"; exit 0; fi
        done
        exit 1
        """
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let t = Process()
            t.executableURL = URL(fileURLWithPath: "/bin/bash")
            t.arguments = ["-c", sh]
            var ok = false
            do { try t.run(); t.waitUntilExit(); ok = (t.terminationStatus == 0) } catch { ok = false }
            DispatchQueue.main.async {
                guard let self = self, self.webLoaded else { return }
                self.web.evaluateJavaScript("window.__openDone(\(ok))")
            }
        }
    }
}

var globalDelegate: AppDelegate?   // Carbon 热键回调是 C 函数指针,无法捕获上下文,走全局引用

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // 无 dock 图标,纯菜单栏 + 浮窗
let delegate = AppDelegate()
globalDelegate = delegate
app.delegate = delegate
app.run()
