// swift-tools-version:5.9
// Plain movement logic and MOVEMENT record encoding: `swift test` here, on
// the Mac or Linux. The app itself builds from Hearsay.xcodeproj.
import PackageDescription

let package = Package(
    name: "HearsayLogic",
    targets: [
        .target(name: "HearsayLogic", path: "Hearsay", sources: ["Movement.swift"]),
        .testTarget(name: "MovementTests", dependencies: ["HearsayLogic"], path: "Tests"),
    ]
)
