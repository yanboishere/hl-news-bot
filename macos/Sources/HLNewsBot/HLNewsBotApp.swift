import SwiftUI
import AppKit

@main
struct HLNewsBotApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var server = ServerController()

    var body: some Scene {
        WindowGroup("hl-news-bot") {
            RootView()
                .environmentObject(server)
                .frame(minWidth: 1040, minHeight: 700)
                .onAppear { server.startIfNeeded() }
        }
        .windowStyle(.titleBar)
        .windowToolbarStyle(.unified(showsTitle: false))
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("服务") {
                Button("重启本地服务") { server.restart() }.keyboardShortcut("r", modifiers: [.command, .shift])
                Button("在浏览器中打开") { server.openInBrowser() }.keyboardShortcut("o", modifiers: [.command, .shift])
                Divider()
                Button("选择仓库目录…") { server.chooseRepo() }
                Button("选择 Python 解释器…") { server.choosePython() }
                Divider()
                Button("打开 data 目录") { server.revealData() }
                Button("查看服务日志") { server.revealServerLog() }
            }
        }
        Settings { SettingsView().environmentObject(server) }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        // The Python UI server owns the bot child process and stops it (flattening positions) on SIGINT.
        ServerController.shared?.stop(sync: true)
    }
}
