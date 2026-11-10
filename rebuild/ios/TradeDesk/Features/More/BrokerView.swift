import AuthenticationServices
import SwiftUI

/// Kite API key (once) and the daily login. The key and secret go to the server over HTTPS, are stored
/// encrypted there and are never shown again. No orders are possible in this version.
struct BrokerView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true
    @State private var status: KiteStatus?
    @State private var clientID = ""
    @State private var apiKey = ""
    @State private var apiSecret = ""
    @State private var editing = false
    @State private var message: String?
    @State private var checking = false
    @State private var confirmRemove = false

    var body: some View {
        Form {
            if let s = status, s.setUp, !editing {
                Section("Today") {
                    LabeledContent("Client", value: s.clientId ?? "—")
                    if s.connectedToday {
                        Label("Logged in for today", systemImage: "checkmark.seal.fill").foregroundStyle(Color.tdGood)
                    } else {
                        Label("Login needed", systemImage: "exclamationmark.triangle.fill").foregroundStyle(Color.tdWarn)
                    }
                    if let t = s.lastLoginAt { LabeledContent("Last login", value: Fmt.dayTime(t, inIST: showIST)) }
                    KiteLoginButton { await load() }
                    Button {
                        Task { await check() }
                    } label: {
                        HStack { Text("Check the session with Zerodha"); if checking { Spacer(); ProgressView() } }
                    }
                    .disabled(!s.connectedToday || checking)
                }
                Section {
                    Button("Change API key") { editing = true }
                    Button("Remove Kite from TradeDesk", role: .destructive) { confirmRemove = true }
                        .confirmationDialog("Remove your Kite API key?", isPresented: $confirmRemove, titleVisibility: .visible) {
                            Button("Remove", role: .destructive) { Task { await remove() } }
                        } message: {
                            Text("Today's login ends too. You will need the key and secret again to log in.")
                        }
                } footer: {
                    Text("Kite sessions end at 6:00 IST every day (SEBI rules), so log in each trading morning. "
                         + "Reminders come at 8:30 and 8:45 IST if you have not.")
                }
            } else {
                Section {
                    TextField("Client ID (e.g. AB1234)", text: $clientID).textInputAutocapitalization(.characters)
                    TextField("API key", text: $apiKey).font(.body.monospaced())
                    SecureField("API secret", text: $apiSecret)
                } header: {
                    Text("Your Kite Connect app")
                } footer: {
                    Text("From developers.kite.trade › your app. Set its redirect URL to \(redirectURL). "
                         + "The secret is stored encrypted on the server and never shown again.")
                }
                Button("Save") { Task { await save() } }
                    .disabled(clientID.count < 4 || apiKey.count < 6 || apiSecret.count < 6)
                if editing { Button("Cancel") { editing = false } }
            }
            if let message { Text(message).font(.footnote).foregroundStyle(.secondary) }
        }
        .navigationTitle("Zerodha")
        .task { await load() }
    }

    private var redirectURL: String { (model.serverURL?.absoluteString ?? "https://your-server") + "/kite/callback" }

    private func load() async {
        status = try? await model.api.get("/v1/broker/kite")
        try? await model.refresh()
    }

    private func save() async {
        struct Body: Encodable { let clientId, apiKey, apiSecret: String }
        do {
            let _: OK = try await model.api.put("/v1/broker/kite", Body(clientId: clientID, apiKey: apiKey, apiSecret: apiSecret))
            apiSecret = ""; apiKey = ""; editing = false
            message = "Saved. Now log in to Kite."
            await load()
        } catch { message = error.localizedDescription }
    }

    private func remove() async {
        struct Resp: Decodable { let ok: Bool }
        do {
            let _: Resp = try await model.api.send("DELETE", "/v1/broker/kite", body: Optional<String>.none)
            message = "Removed."
            await load()
        } catch { message = error.localizedDescription }
    }

    private func check() async {
        struct Resp: Decodable { let ok: Bool; let userName: String?; let reason: String? }
        checking = true; defer { checking = false }
        do {
            let r: Resp = try await model.api.post("/v1/broker/kite/check")
            message = r.ok ? "Zerodha confirms the session for \(r.userName ?? "you")." : (r.reason ?? "Not logged in.")
        } catch { message = error.localizedDescription }
    }
}

/// Opens Zerodha's login page in a secure in-app browser. The server finishes the login and sends the
/// browser back to tradedesk://kite?status=..., which closes the sheet.
struct KiteLoginButton: View {
    @Environment(AppModel.self) private var model
    @Environment(\.webAuthenticationSession) private var webAuth
    var done: (() async -> Void)?
    @State private var working = false
    @State private var result: String?

    init(done: (() async -> Void)? = nil) { self.done = done }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                Task { await login() }
            } label: {
                Label(working ? "Opening Zerodha…" : "Log in to Kite", systemImage: "person.badge.key")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.glassProminent)
            .disabled(working)
            if let result { Text(result).font(.footnote).foregroundStyle(.secondary) }
        }
    }

    private func login() async {
        working = true; defer { working = false }
        do {
            let r: LoginURL = try await model.api.post("/v1/broker/kite/login-url")
            let back = try await webAuth.authenticate(using: r.url, callbackURLScheme: r.callbackScheme)
            let status = URLComponents(url: back, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "status" }?.value ?? "unknown"
            result = switch status {
            case "ok": "Logged in for today."
            case "cancelled": "Login cancelled."
            case "expired": "The login took too long. Try again."
            default: "Zerodha did not accept the login. Try again."
            }
            try? await model.refresh()
            await done?()
        } catch let e as ASWebAuthenticationSessionError where e.code == .canceledLogin {
            result = nil
        } catch {
            result = error.localizedDescription
        }
    }
}
