import SwiftUI

/// The free Apple account has no push, so alerts reach the phone through Telegram and ntfy.
struct AlertSettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var info: ChannelsInfo?
    @State private var prefs: [AlertPref] = []
    @State private var telegramStarted = false
    @Environment(\.openURL) private var openURL
    @State private var ntfyTopic = ""
    @State private var message: String?

    var body: some View {
        Form {
            Section {
                if let t = info?.telegram { LabeledContent("Chat", value: t.target) }
                Button(info?.telegram == nil ? "Connect Telegram" : "Connect again", systemImage: "link") {
                    Task { await startTelegram() }
                }
                .disabled(info?.telegramBotReady != true)
                if telegramStarted {
                    Button("I tapped Start in Telegram", systemImage: "checkmark.circle") { Task { await checkTelegram() } }
                }
            } header: {
                Label("Telegram", systemImage: "paperplane")
            } footer: {
                Text(info?.telegramBotReady == false
                     ? "The server has no Telegram bot yet (TD2_TELEGRAM_BOT_TOKEN)."
                     : "Opens the TradeDesk bot in Telegram. Tap Start there, then come back and confirm.")
            }
            Section {
                if let n = info?.ntfy { LabeledContent("Topic", value: n.target).font(.footnote.monospaced()) }
                HStack {
                    TextField("Topic", text: $ntfyTopic).font(.body.monospaced()).autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                    Button("New") { Task { await newTopic() } }
                }
                Button("Save ntfy") { Task { await save("ntfy", ntfyTopic) } }.disabled(ntfyTopic.count < 16)
            } header: {
                Label("ntfy", systemImage: "bell.and.waves.left.and.right")
            } footer: {
                Text("Install the ntfy app, subscribe to this topic on \(info?.ntfyServer ?? "ntfy.sh"), and allow its "
                     + "notifications to break through Focus. Keep the topic secret: anyone who knows it can read your alerts.")
            }
            Section {
                ForEach($prefs) { $p in
                    Toggle(p.category.replacingOccurrences(of: "_", with: " ").capitalized, isOn: Binding(
                        get: { !p.muted },
                        set: { on in
                            $p.wrappedValue.muted = !on
                            let updated = $p.wrappedValue
                            Task { await savePref(updated) }
                        }))
                }
            } header: {
                Text("Send messages for")
            } footer: {
                Text("Turning a category off stops its messages; alerts still show in the app. Critical alerts are always sent and repeat every 2 minutes until you acknowledge them.")
            }
            Section {
                Button("Send a test (important)") { Task { await test("important") } }
                Button("Send a test (critical, repeats)") { Task { await test("critical") } }
            }
            if let message { Text(message).font(.footnote).foregroundStyle(.secondary) }
        }
        .navigationTitle("Alerts")
        .task { await load() }
    }

    private func load() async {
        do {
            info = try await model.api.get("/v1/alerts/channels")
            prefs = try await model.api.get("/v1/alerts/prefs")
        } catch { message = "Couldn't reach the server: \(error.localizedDescription)" }
    }

    private func startTelegram() async {
        struct Resp: Decodable { let url: URL }
        do {
            let r: Resp = try await model.api.post("/v1/alerts/channels/telegram-link")
            telegramStarted = true
            openURL(r.url)
        } catch { message = error.localizedDescription }
    }

    private func checkTelegram() async {
        struct Resp: Decodable { let ok: Bool; let detail: String? }
        do {
            let r: Resp = try await model.api.post("/v1/alerts/channels/telegram-link/check")
            message = r.ok ? "Telegram connected. A confirmation message is in the chat." : r.detail
            if r.ok { telegramStarted = false; await load() }
        } catch { message = error.localizedDescription }
    }

    private func newTopic() async {
        if let r: TopicResponse = try? await model.api.post("/v1/alerts/channels/ntfy-topic") { ntfyTopic = r.topic }
    }

    private func save(_ kind: String, _ target: String) async {
        struct Body: Encodable { let kind, target: String; let enabled: Bool }
        do {
            let _: OK = try await model.api.put("/v1/alerts/channels", Body(kind: kind, target: target, enabled: true))
            message = "Saved \(kind)."
            await load()
        } catch { message = error.localizedDescription }
    }

    private func savePref(_ p: AlertPref) async {
        struct Body: Encodable { let category: String; let muted: Bool }
        struct Resp: Decodable { let ok: Bool }
        do {
            let _: Resp = try await model.api.put("/v1/alerts/prefs", Body(category: p.category, muted: p.muted))
        } catch {
            if let i = prefs.firstIndex(where: { $0.category == p.category }) { prefs[i].muted = !p.muted }
            message = "Not saved: \(error.localizedDescription)"
        }
    }

    private func test(_ level: String) async {
        do {
            let a: AlertItem = try await model.api.post("/v1/alerts/test?level=\(level)")
            message = "Sent “\(a.title)”. It should arrive within 15 seconds."
            try? await model.refresh()
        } catch { message = error.localizedDescription }
    }
}
