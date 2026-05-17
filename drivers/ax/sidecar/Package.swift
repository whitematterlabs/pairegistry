// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AXSidecar",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "axd", targets: ["AXSidecar"])
    ],
    dependencies: [
        .package(url: "https://github.com/tmandry/AXSwift.git", from: "0.3.2"),
    ],
    targets: [
        .executableTarget(
            name: "AXSidecar",
            dependencies: ["AXSwift"],
            path: "Sources/AXSidecar"
        )
    ]
)
