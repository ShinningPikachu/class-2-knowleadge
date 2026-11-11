// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ClassKnowledgeApp",
    defaultLocalization: "en",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "ClassKnowledgeApp", targets: ["ClassKnowledgeApp"]),
    ],
    targets: [
        .executableTarget(
            name: "ClassKnowledgeApp",
            path: "Sources/ClassKnowledgeApp"
        ),
    ],
    swiftLanguageModes: [.v5]
)
