// swift-tools-version:5.9
// macOS shell for hl-news-bot.
//   cd macos && ./build-app.sh        -> build/HLNewsBot.app   (recommended)
//   or: swift build -c release && .build/release/HLNewsBot    (bare binary; Info.plist is embedded so ATS allows 127.0.0.1)
//   or: open Package.swift in Xcode and run the HLNewsBot scheme.
import PackageDescription

let package = Package(
    name: "HLNewsBot",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "HLNewsBot",
            path: "Sources/HLNewsBot",
            exclude: ["Resources/Info.plist"],
            linkerSettings: [
                // Embed Info.plist in the __TEXT,__info_plist section so the un-bundled binary also carries
                // NSAllowsLocalNetworking (macOS 14 ATS blocks plain IP connections without it). When wrapped
                // into an .app by build-app.sh, Contents/Info.plist takes precedence.
                .unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
                              "-Xlinker", "\(Context.packageDirectory)/Sources/HLNewsBot/Resources/Info.plist"])
            ]
        )
    ]
)
