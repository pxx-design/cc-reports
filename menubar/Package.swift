// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ccglance",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "ccglance", path: "Sources/ccglance")
    ]
)
