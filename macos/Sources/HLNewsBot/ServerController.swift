import Foundation
import AppKit
import Combine

/// Launches and supervises `python -m hlnews.ui` (the local HTTP backend), and knows where the repo is.
/// The web console inside the window is the real UI; this class only manages the process behind it.
@MainActor
final class ServerController: ObservableObject {
    static var shared: ServerController?

    enum Status: Equatable { case idle, starting, running, failed(String) }

    @Published var status: Status = .idle
    @Published var repoPath: String {
        didSet { UserDefaults.standard.set(repoPath, forKey: "repoPath") }
    }
    @Published var pythonPath: String {
        didSet { UserDefaults.standard.set(pythonPath, forKey: "pythonPath") }
    }
    @Published var port: Int {
        didSet { UserDefaults.standard.set(port, forKey: "port") }
    }

    private var process: Process?
    private var logHandle: FileHandle?
    private var healthTimer: Timer?

    var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }
    var serverLogURL: URL { URL(fileURLWithPath: repoPath).appendingPathComponent("data/ui-server.log") }

    init() {
        let d = UserDefaults.standard
        repoPath = d.string(forKey: "repoPath") ?? ServerController.guessRepo()
        pythonPath = d.string(forKey: "pythonPath") ?? ServerController.guessPython(repo: d.string(forKey: "repoPath") ?? ServerController.guessRepo())
        port = d.integer(forKey: "port") == 0 ? 8765 : d.integer(forKey: "port")
        ServerController.shared = self
    }

    // MARK: discovery

    /// The app is normally built from <repo>/macos; walk up from the executable until config.yaml is found.
    static func guessRepo() -> String {
        var url = Bundle.main.bundleURL
        for _ in 0..<8 {
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("config.yaml").path) { return url.path }
            url.deleteLastPathComponent()
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        for cand in ["hl-news-bot", "Code/hl-news-bot", "Projects/hl-news-bot", "dev/hl-news-bot"] {
            let p = home.appendingPathComponent(cand)
            if FileManager.default.fileExists(atPath: p.appendingPathComponent("config.yaml").path) { return p.path }
        }
        return home.appendingPathComponent("hl-news-bot").path
    }

    /// Prefer the repo's own venv, then common Homebrew/system locations.
    static func guessPython(repo: String) -> String {
        let cands = [
            "\(repo)/.venv/bin/python", "\(repo)/venv/bin/python",
            "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3",
        ]
        return cands.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
    }

    // MARK: lifecycle

    private var isStopping = false

    func startIfNeeded() {
        if case .running = status { return }
        if case .starting = status { return }
        start()
    }

    func start() {
        guard !isStopping else { return }   // a restart requested while the previous server is still shutting down
        stop(sync: true)
        guard FileManager.default.fileExists(atPath: URL(fileURLWithPath: repoPath).appendingPathComponent("config.yaml").path) else {
            status = .failed("在 \(repoPath) 里没有找到 config.yaml。用「服务 › 选择仓库目录」指向 hl-news-bot 仓库。")
            return
        }
        guard FileManager.default.isExecutableFile(atPath: pythonPath) else {
            status = .failed("Python 解释器不存在：\(pythonPath)。用「服务 › 选择 Python 解释器」指定一个装了 requirements.txt 的解释器。")
            return
        }
        status = .starting
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = ["-m", "hlnews.ui", "--port", String(port), "--repo", repoPath, "--python", pythonPath]
        p.currentDirectoryURL = URL(fileURLWithPath: repoPath)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        // Load <repo>/.env (KEY=VALUE lines) so HL_AGENT_KEY / HL_ACCOUNT / ANTHROPIC_API_KEY reach the bot without
        // living in the shell profile. The file is git-ignored.
        for (k, v) in ServerController.readDotEnv(URL(fileURLWithPath: repoPath).appendingPathComponent(".env")) where env[k] == nil { env[k] = v }
        p.environment = env

        let dataDir = URL(fileURLWithPath: repoPath).appendingPathComponent("data")
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: serverLogURL.path) { FileManager.default.createFile(atPath: serverLogURL.path, contents: nil) }
        if let h = try? FileHandle(forWritingTo: serverLogURL) {
            h.seekToEndOfFile()
            p.standardOutput = h
            p.standardError = h
            logHandle = h
        }
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in
                guard let self, self.process === proc else { return }  // exit of a previous server instance: ignore
                if case .running = self.status { self.status = .failed("本地服务退出了（code \(proc.terminationStatus)）。看「服务 › 查看服务日志」。") }
                else if case .starting = self.status { self.status = .failed("本地服务启动失败（code \(proc.terminationStatus)）。通常是依赖没装（pip install -r requirements.txt）或端口被占用。\n\n服务日志尾部：\n\(self.logTail())") }
                self.process = nil
            }
        }
        do {
            try p.run()
            process = p
            waitForHealth(attempt: 0)
        } catch {
            status = .failed("无法启动 Python：\(error.localizedDescription)")
        }
    }

    private func waitForHealth(attempt: Int) {
        var req = URLRequest(url: url.appendingPathComponent("api/state"))
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, _ in
            Task { @MainActor in
                guard let self else { return }
                if let http = resp as? HTTPURLResponse, http.statusCode == 200, data != nil {
                    self.status = .running
                    self.startHealthTimer()
                } else if attempt < 40, self.process?.isRunning == true {
                    try? await Task.sleep(nanoseconds: 250_000_000)
                    self.waitForHealth(attempt: attempt + 1)
                } else if self.process?.isRunning == true {
                    self.status = .failed("服务进程在跑，但 \(self.url.absoluteString) 没有响应。端口可能被占用；在设置里换一个端口。")
                }
            }
        }.resume()
    }

    private func startHealthTimer() {
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, case .running = self.status else { return }
                if let proc = self.process, !proc.isRunning { self.status = .failed("本地服务已退出。") }
            }
        }
    }

    func stop(sync: Bool) {
        healthTimer?.invalidate()
        healthTimer = nil
        guard let p = process else { return }
        process = nil            // detach first so re-entrant callers and the old terminationHandler see no live process
        let h = logHandle
        logHandle = nil
        status = .idle
        if p.isRunning {
            p.interrupt()        // SIGINT -> ui.py KeyboardInterrupt -> finally: bot.stop() (SIGINT to the bot, waits up to 15 s, then SIGTERM)
            if sync {
                isStopping = true
                defer { isStopping = false }
                let deadline = Date().addingTimeInterval(20)   // must exceed ui.py's 15 s + 1 s bot shutdown budget
                while p.isRunning && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.1)) }
                if p.isRunning { p.terminate() }
            }
        }
        try? h?.close()
    }

    /// Last few lines of data/ui-server.log, for failure messages.
    func logTail(lines: Int = 12) -> String {
        guard let text = try? String(contentsOf: serverLogURL, encoding: .utf8) else { return "（无日志）" }
        return text.split(separator: "\n").suffix(lines).joined(separator: "\n")
    }

    func restart() { start() }

    // MARK: menu actions

    func openInBrowser() { NSWorkspace.shared.open(url) }

    func chooseRepo() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "选择 hl-news-bot 仓库目录（里面有 config.yaml）"
        if panel.runModal() == .OK, let u = panel.url {
            repoPath = u.path
            pythonPath = ServerController.guessPython(repo: repoPath)
            start()
        }
    }

    func choosePython() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.showsHiddenFiles = true
        panel.allowsMultipleSelection = false
        panel.message = "选择 python 可执行文件（建议仓库下 .venv/bin/python）"
        if panel.runModal() == .OK, let u = panel.url {
            pythonPath = u.path
            start()
        }
    }

    func revealData() { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: repoPath).appendingPathComponent("data")]) }
    func revealServerLog() { NSWorkspace.shared.activateFileViewerSelecting([serverLogURL]) }

    // MARK: helpers

    static func readDotEnv(_ url: URL) -> [String: String] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
        var out: [String: String] = [:]
        for raw in text.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            let body = line.hasPrefix("export ") ? String(line.dropFirst(7)) : line
            guard let eq = body.firstIndex(of: "=") else { continue }
            let k = body[..<eq].trimmingCharacters(in: .whitespacesAndNewlines)
            var v = body[body.index(after: eq)...].trimmingCharacters(in: .whitespacesAndNewlines)
            if v.count >= 2, (v.hasPrefix("\"") && v.hasSuffix("\"")) || (v.hasPrefix("'") && v.hasSuffix("'")) { v = String(v.dropFirst().dropLast()) }
            if !k.isEmpty && !v.isEmpty { out[k] = v }
        }
        return out
    }
}
