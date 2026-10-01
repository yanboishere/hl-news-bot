import SwiftUI
import WebKit

struct RootView: View {
    @EnvironmentObject var server: ServerController

    var body: some View {
        Group {
            switch server.status {
            case .running:
                WebConsole(url: server.url)
            case .starting, .idle:
                StatusPane(title: "正在启动本地服务", detail: "python -m hlnews.ui --port \(server.port)\n仓库：\(server.repoPath)\n解释器：\(server.pythonPath)", showSpinner: true)
            case .failed(let msg):
                StatusPane(title: "本地服务没有起来", detail: msg, showSpinner: false)
            }
        }
        .toolbar {
            ToolbarItemGroup(placement: .automatic) {
                Button { server.restart() } label: { Label("重启服务", systemImage: "arrow.clockwise") }
                    .help("重启本地服务（不影响 bot 进程的持仓，bot 会被一起重启）")
                Button { server.openInBrowser() } label: { Label("浏览器", systemImage: "safari") }
                    .help("在默认浏览器里打开同一个控制台")
            }
        }
        .background(Color(nsColor: NSColor(calibratedRed: 0.984, green: 0.980, blue: 0.969, alpha: 1)))
    }
}

struct StatusPane: View {
    @EnvironmentObject var server: ServerController
    let title: String
    let detail: String
    let showSpinner: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Spacer()
            HStack(spacing: 10) {
                if showSpinner { ProgressView().controlSize(.small) }
                Text(title).font(.system(size: 22, weight: .semibold, design: .serif))
            }
            Text(detail)
                .font(.system(size: 13, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .frame(maxWidth: 640, alignment: .leading)
            if !showSpinner {
                HStack(spacing: 10) {
                    Button("重试") { server.restart() }
                    Button("选择仓库目录…") { server.chooseRepo() }
                    Button("选择 Python…") { server.choosePython() }
                    Button("查看服务日志") { server.revealServerLog() }
                }
                .padding(.top, 6)
                Text("第一次运行前，在仓库目录执行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .padding(.top, 4)
            }
            Spacer()
            Spacer()
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

/// WKWebView wrapper. Opens external links (e.g. the 原文 link on a headline) in the default browser.
struct WebConsole: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.defaultWebpagePreferences.allowsContentJavaScript = true
        if #unavailable(macOS 13.3), cfg.preferences.responds(to: Selector(("_setDeveloperExtrasEnabled:"))) {
            cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")   // Web Inspector on 13.0–13.2
        }
        let wv = WKWebView(frame: .zero, configuration: cfg)
        if #available(macOS 13.3, *) { wv.isInspectable = true }               // right-click › Inspect Element
        wv.navigationDelegate = context.coordinator
        wv.uiDelegate = context.coordinator
        if wv.responds(to: Selector(("_setDrawsBackground:"))) { wv.setValue(false, forKey: "drawsBackground") }  // let the paper background through while loading
        wv.load(URLRequest(url: url))
        return wv
    }

    func updateNSView(_ wv: WKWebView, context: Context) {
        if wv.url?.host != url.host || wv.url?.port != url.port { wv.load(URLRequest(url: url)) }
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if let u = navigationAction.request.url, let host = u.host, host != "127.0.0.1" && host != "localhost" {
                NSWorkspace.shared.open(u)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }
        // target=_blank links
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let u = navigationAction.request.url { NSWorkspace.shared.open(u) }
            return nil
        }
        // window.alert / confirm used by the console for one-line errors
        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let a = NSAlert(); a.messageText = message; a.runModal(); completionHandler()
        }
        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
            completionHandler(a.runModal() == .alertFirstButtonReturn)
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject var server: ServerController
    @State private var portText: String = ""

    var body: some View {
        Form {
            Section("本地服务") {
                LabeledContent("仓库目录") {
                    HStack { Text(server.repoPath).lineLimit(1).truncationMode(.middle).textSelection(.enabled); Button("选择…") { server.chooseRepo() } }
                }
                LabeledContent("Python") {
                    HStack { Text(server.pythonPath).lineLimit(1).truncationMode(.middle).textSelection(.enabled); Button("选择…") { server.choosePython() } }
                }
                LabeledContent("端口") {
                    HStack {
                        TextField("8765", text: $portText).frame(width: 90).onAppear { portText = String(server.port) }
                        Button("应用并重启") { if let p = Int(portText), (1024...65535).contains(p) { server.port = p; server.restart() } }
                    }
                }
            }
            Section {
                Text("实盘密钥不在这里填。把 HL_AGENT_KEY、HL_ACCOUNT（以及可选的 ANTHROPIC_API_KEY）写进仓库目录下的 .env 文件，应用启动服务时会读取它并传给 bot 进程。.env 已在 .gitignore 里。")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(width: 560)
        .padding()
    }
}
