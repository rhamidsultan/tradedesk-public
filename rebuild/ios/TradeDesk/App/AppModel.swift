import Foundation
import Observation
import UIKit

enum AppTab: Hashable { case home, strategies, trade, orders, more }

enum Route: Hashable {
    case alerts, broker, alertSettings, devices, about, settings, data, recorder, health, journal
    case portfolio, portfolioStock(String), portfolioGold, us, users, userDetail(Int), share
    case dataIndex(String)
    case dataDay(String, String)
    case chain(String, String, String)
    case contract(String, String, String)
}

/// App-wide state: sign-in, the server status snapshot and navigation from links (Telegram/ntfy/widgets).
@Observable @MainActor
final class AppModel {
    enum Phase: Equatable { case loading, onboarding, locked, ready }

    var phase: Phase = .loading
    var user: UserInfo?
    var status: StatusSnapshot?
    var openAlerts: [AlertItem] = []
    var indexes: IndexStrip?
    var latestRuns: [BacktestRunItem] = []
    var lastError: String?
    var lastRefresh: Date?
    /// True when the last attempt to reach the server failed: screens grey out and show the data's age.
    var connectionProblem = false
    var tab: AppTab = .home
    var homePath: [Route] = []
    var ordersMode = "live"          // Orders tab: LIVE or SHADOW (the strip's LIVE pill opens LIVE)
    var morePath: [Route] = []
    /// What this login may see or use per area (none | view | use), from /v1/me. Empty until loaded.
    var access: [String: String] = (UserDefaults.standard.dictionary(forKey: "access") as? [String: String]) ?? [:] {
        didSet { UserDefaults.standard.set(access, forKey: "access") }
    }
    /// The owner set Hide amounts for this login: amounts arrive scaled from the server and stay hidden here.
    var forcedHideAmounts = UserDefaults.standard.bool(forKey: "forcedHideAmounts") {
        didSet { UserDefaults.standard.set(forcedHideAmounts, forKey: "forcedHideAmounts") }
    }

    /// Owner: everything. Anyone else: only what /v1/me said (kept between launches); nothing until it is known.
    func can(_ area: String, _ level: String = "view") -> Bool {
        if user?.role == "owner" { return true }
        guard let a = access[area] else { return false }
        return level == "use" ? a == "use" : a != "none"
    }
    var busy = false

    let api = APIClient()
    private var pollTask: Task<Void, Never>?

    private let defaults = UserDefaults.standard
    private enum K { static let server = "serverURL", keyID = "keyID", user = "user", token = "session-token" }

    var serverURL: URL? {
        get { defaults.string(forKey: K.server).flatMap(URL.init(string:)) }
        set { defaults.set(newValue?.absoluteString, forKey: K.server); api.baseURL = newValue }
    }

    var installExpiry: Date? { Provisioning.expiryDate() }

    // MARK: start-up

    func start() async {
        api.baseURL = serverURL
        guard serverURL != nil, DeviceKey.exists, defaults.string(forKey: K.keyID) != nil else {
            phase = .onboarding
            return
        }
        if let data = defaults.data(forKey: K.user) { user = try? JSONDecoder().decode(UserInfo.self, from: data) }
        if let token = Keychain.string(for: K.token) {
            api.token = token
            do {
                try await refresh()
                phase = .ready
                startPolling()
                await reportInstall()
                return
            } catch APIError.unauthorized {
                // session ended: fall through to Face ID
            } catch {
                // offline: show the app with the last known state rather than locking the user out
                phase = .ready
                lastError = error.localizedDescription
                startPolling()
                return
            }
        }
        phase = .locked
    }

    // MARK: enrol and sign in

    func enroll(server: String, invite: String, deviceName: String) async {
        busy = true; defer { busy = false }
        lastError = nil
        guard let url = Self.normalise(server) else { lastError = "Enter the server address, e.g. https://trade.example.com"; return }
        serverURL = url
        do {
            let key = try DeviceKey.create()
            let code = Self.normaliseInvite(invite)
            let sig = try key.signature(for: SignMessage.enroll(code: code), reason: "Create your TradeDesk sign-in key")
            struct Body: Encodable { let inviteCode, deviceName, publicKey, signature: String }
            let r: EnrollResponse = try await api.post("/v1/auth/enroll", Body(
                inviteCode: code, deviceName: deviceName,
                publicKey: key.publicKeyX963.base64EncodedString(), signature: sig.base64EncodedString()))
            defaults.set(r.keyId, forKey: K.keyID)
            await signIn()
        } catch {
            DeviceKey.remove()
            lastError = error.localizedDescription
        }
    }

    /// Face ID signature with this device's key (arming live trading).
    func sign(_ message: Data, reason: String) throws -> String {
        guard let key = try DeviceKey.load() else { throw APIError.http(401, "This device has no sign-in key.") }
        return try key.signature(for: message, reason: reason).base64EncodedString()
    }

    func signIn() async {
        busy = true; defer { busy = false }
        lastError = nil
        do {
            guard let key = try DeviceKey.load(), let keyID = defaults.string(forKey: K.keyID) else {
                phase = .onboarding; return
            }
            struct ChallengeBody: Encodable { let keyId: String }
            let ch: ChallengeResponse = try await api.post("/v1/auth/challenge", ChallengeBody(keyId: keyID))
            let sig = try key.signature(for: SignMessage.login(challengeID: ch.challengeId, nonce: ch.nonce),
                                        reason: "Sign in to TradeDesk")
            struct LoginBody: Encodable {
                let challengeId, signature: String
                let installExpiresAt: Date?
                let appVersion: String?
            }
            let r: LoginResponse = try await api.post("/v1/auth/login", LoginBody(
                challengeId: ch.challengeId, signature: sig.base64EncodedString(),
                installExpiresAt: installExpiry, appVersion: Self.appVersion))
            try Keychain.set(r.token, for: K.token)
            api.token = r.token
            user = r.user
            defaults.set(try? JSONEncoder().encode(r.user), forKey: K.user)
            try? await refresh()
            phase = .ready
            startPolling()
            if let exp = installExpiry { await Provisioning.scheduleReminder(expiry: exp) }
        } catch APIError.unauthorized(let m) {
            lastError = m
            if m.contains("not enrolled") { resetDevice() }
        } catch {
            lastError = error.localizedDescription
        }
    }

    func signOut() async {
        let _: OK? = try? await api.post("/v1/auth/logout")
        Keychain.delete(K.token)
        api.token = nil
        pollTask?.cancel()
        phase = .locked
    }

    /// Forget this device's key (after the owner revoked it, or to enrol again).
    func resetDevice() {
        pollTask?.cancel()
        DeviceKey.remove()
        Keychain.delete(K.token)
        defaults.removeObject(forKey: K.keyID)
        defaults.removeObject(forKey: K.user)
        api.token = nil
        user = nil
        access = [:]
        forcedHideAmounts = false
        status = nil
        phase = .onboarding
    }

    // MARK: data

    func refresh() async throws {
        do {
            status = try await api.get("/v1/status")
            openAlerts = try await api.get("/v1/alerts?open_only=true")
            if let me = try? await api.raw("GET", "/v1/me") {
                let acc = (me["access"]?.object ?? [:]).compactMapValues(\.string)
                if acc != access { access = acc }
                forcedHideAmounts = me["hide_amounts"]?.bool == true
                if forcedHideAmounts { UserDefaults.standard.set(true, forKey: "hideAmounts") }
            }
            if can("market_data") {
                indexes = try? await api.get("/v1/market/indexes")
            } else {
                indexes = nil
            }
            if can("backtests"), let runs: [BacktestRunItem] = try? await api.get("/v1/research/backtests") {
                latestRuns = Array(runs.filter { $0.status == "done" }.prefix(3))
            }
            lastRefresh = .now
            lastError = nil
            connectionProblem = false
        } catch APIError.unauthorized {
            phase = .locked
            throw APIError.unauthorized("Session ended")
        } catch {
            connectionProblem = true
            lastError = error.localizedDescription
            throw error
        }
    }

    /// Seconds since the last good refresh, or nil if there never was one.
    var dataAge: TimeInterval? { lastRefresh.map { Date.now.timeIntervalSince($0) } }
    var isStale: Bool { connectionProblem || (dataAge ?? .infinity) > 45 }

    func ack(_ alert: AlertItem) async {
        await ack([alert])
    }

    func ack(_ alerts: [AlertItem]) async {
        do {
            for a in alerts { let _: AlertItem = try await api.post("/v1/alerts/\(a.id)/ack") }
            try await refresh()
        } catch { lastError = error.localizedDescription }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                guard let self, UIApplication.shared.applicationState == .active else { continue }
                try? await self.refresh()
            }
        }
    }

    private func reportInstall() async {
        guard let exp = installExpiry else { return }
        struct Body: Encodable { let installExpiresAt: Date; let appVersion: String? }
        let _: OK? = try? await api.patch("/v1/devices/this", Body(installExpiresAt: exp, appVersion: Self.appVersion))
        await Provisioning.scheduleReminder(expiry: exp)
    }

    // MARK: links from Telegram, ntfy and the web console: tradedesk://<path>

    func open(_ url: URL) {
        guard url.scheme == "tradedesk" else { return }
        let path = ([url.host ?? ""] + url.pathComponents.filter { $0 != "/" }).joined(separator: "/")
        switch path {
        case "alerts": tab = .home; homePath = [.alerts]
        case "broker/kite", "broker": tab = .more; morePath = [.broker]
        case "more/about": tab = .more; morePath = [.about]
        case "data": tab = .more; morePath = [.data]
        case "portfolio" where can("portfolio"): tab = .more; morePath = [.portfolio]
        case "us" where can("us"): tab = .more; morePath = [.us]
        case "more/health": tab = .more; morePath = [.health]
        case "kite": Task { try? await refresh() }
        default: tab = .home
        }
    }

    // MARK: helpers

    nonisolated static var appVersion: String? { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String }

    nonisolated static func normalise(_ server: String) -> URL? {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        func isLocal(_ host: String) -> Bool {
            if host == "localhost" || host.hasSuffix(".local") || host.hasPrefix("192.168.") || host.hasPrefix("10.") { return true }
            let p = host.split(separator: ".").compactMap { Int($0) }
            return p.count == 4 && p[0] == 172 && (16...31).contains(p[1])
        }
        if !s.contains("://") {
            // a home-network address (the Mac trial) is plain http; anything else must be https
            let host = String(s.split(separator: "/").first?.split(separator: ":").first ?? "")
            s = (isLocal(host) ? "http://" : "https://") + s
        }
        guard let url = URL(string: s), let host = url.host, !host.isEmpty else { return nil }
        guard url.scheme == "https" || (url.scheme == "http" && isLocal(host)) else { return nil }
        return url
    }

    nonisolated static func normaliseInvite(_ code: String) -> String {
        let c = code.uppercased().filter { $0.isLetter || $0.isNumber }
        guard c.count == 12 else { return code.uppercased() }
        let a = Array(c)
        return "\(String(a[0..<4]))-\(String(a[4..<8]))-\(String(a[8..<12]))"
    }
}
