// swift-tools-version:5.9
// The app's plain logic (movement and MOVEMENT record encoding, where a
// conversation began): `swift test` here, on the Mac or Linux. The app itself
// builds from Hearsay.xcodeproj.
import PackageDescription

let package = Package(
    name: "HearsayLogic",
    targets: [
        .target(name: "HearsayLogic", path: "Hearsay", sources: ["Movement.swift", "ConversationStart.swift"]),
        .testTarget(name: "LogicTests", dependencies: ["HearsayLogic"], path: "Tests"),
    ]
)
