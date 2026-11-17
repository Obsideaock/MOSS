// swift-tools-version: 6.0
//
// MOSS Radio: the iPhone side of MOSS. Range-tests the radio link to a Seed
// Vault and reads Wikipedia from it, over a MeshCore radio on Bluetooth.
// Built with xtool (see README.md); no dependencies beyond Apple's SDK.

import PackageDescription

let package = Package(
    name: "MossRadio",
    platforms: [
        .iOS(.v17),
    ],
    products: [
        .library(name: "MossRadio", targets: ["MossRadio"]),
    ],
    targets: [
        .target(name: "MossRadio"),
    ],
    swiftLanguageModes: [.v5]
)
