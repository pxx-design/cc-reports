class CcGlance < Formula
  desc "Claude Code usage widget + local daily/weekly/monthly report dashboard"
  homepage "https://github.com/pxx-design/cc-reports"
  url "https://github.com/pxx-design/cc-reports/archive/refs/tags/v0.5.0.tar.gz"
  sha256 "62786f911ae16e44a491956d0a7e51800ade2c6a4896de86e26cef4c25c3b1b6"
  license "MIT"
  head "https://github.com/pxx-design/cc-reports.git", branch: "main"

  depends_on xcode: ["15.0", :build]
  depends_on macos: :ventura      # AppKit / Carbon 热键路径需要 13+
  # 不依赖 brew 的 python:内核是纯标准库,实测系统自带的 python3(3.9.6)跑 build/serve/glance/doctor
  # 全通 —— 让用户为此多装 60MB 的 python@3.13 是白付。浮窗内部也走同一条 `env python3`,口径一致。

  def install
    system "swift", "build", "--disable-sandbox", "-c", "release", "--package-path", "menubar"

    # Python 内核 + dashboard 模板整套进 libexec —— 浮窗按「可执行文件同目录」找它(见 main.swift scriptPath)
    libexec.install "cc-reports.py", "cc-reports.html", "cc_usage_core", "config.example.json"
    libexec.install Dir["menubar/.build/release/ccglance", "menubar/.build/release/*.bundle"]

    # 不用 symlink:SPM 的 Bundle.module 按「可执行文件所在目录」找资源包(glance.html),
    # 从 bin 软链进来会把 bundleURL 指到 bin,资源就丢了。用 shim 直接 exec libexec 里的真身。
    (bin/"cc-glance").write <<~SH
      #!/bin/bash
      exec "#{libexec}/ccglance" "$@"
    SH
    (bin/"cc-reports").write <<~SH
      #!/bin/bash
      exec /usr/bin/env python3 "#{libexec}/cc-reports.py" "$@"
    SH
    chmod 0755, bin/"cc-glance"
    chmod 0755, bin/"cc-reports"
  end

  service do
    run opt_bin/"cc-glance"
    keep_alive true
    environment_variables PATH: std_service_path_env
    log_path var/"log/cc-glance.log"
    error_log_path var/"log/cc-glance.log"
  end

  def caveats
    <<~EOS
      菜单栏图标 = 进程本身，进程活着才有：

        cc-glance                  # 前台起一个（⌥⇧R 或点菜单栏图标开合浮窗）
        brew services start cc-glance   # 开机自启 + 挂了自动拉起

      日报 dashboard（浏览器）：

        cc-reports serve           # → http://localhost:8765

      100% 本地：只读 ~/.claude/projects 的 jsonl，不联网、不上传。
    EOS
  end

  test do
    assert_match "usage", shell_output("#{bin}/cc-reports --help")
    assert_predicate libexec/"ccglance", :executable?
  end
end
