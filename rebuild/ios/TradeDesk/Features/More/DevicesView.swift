import SwiftUI

struct DevicesView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true
    @State private var devices: [DeviceItem] = []
    @State private var code: CodeResponse?
    @State private var message: String?
    @State private var confirmRevoke: DeviceItem?

    var body: some View {
        List {
            Section {
                ForEach(devices) { d in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(d.name).font(.headline)
                            if d.thisDevice { StatusPill(text: "This device", systemImage: "iphone", color: .tdPaper) }
                            if d.revoked { StatusPill(text: "Removed", systemImage: "xmark", color: .tdBad) }
                        }
                        Group {
                            if let used = d.lastUsedAt { Text("Last used \(Fmt.dayTime(used, inIST: showIST))") }
                            if let days = d.installDaysLeft { Text("Install: \(days) days left") }
                        }
                        .font(.caption).foregroundStyle(.secondary)
                    }
                    .swipeActions {
                        if !d.revoked && !d.thisDevice {
                            Button("Remove", systemImage: "trash", role: .destructive) { confirmRevoke = d }
                        }
                    }
                }
            } footer: {
                Text("Swipe to remove a lost device. Its sign-in stops at once.")
            }
            Section {
                Button("Add another device (Mac, iPad)", systemImage: "plus") { Task { await newCode() } }
                if let code {
                    LabeledContent("Invite code") { Text(code.code).font(.title3.monospaced().bold()).textSelection(.enabled) }
                    ShareLink(item: code.code) { Label("Copy or send the code", systemImage: "square.and.arrow.up") }
                    Text("Valid \(code.expiresInDays) days, one use. Enter it in TradeDesk on the other device.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Devices")
        .task { await load() }
        .refreshable { await load() }
        .confirmationDialog("Remove \(confirmRevoke?.name ?? "")?", isPresented: Binding(
            get: { confirmRevoke != nil }, set: { if !$0 { confirmRevoke = nil } }), titleVisibility: .visible) {
            Button("Remove device", role: .destructive) {
                if let d = confirmRevoke { Task { await revoke(d) } }
            }
        }
    }

    private func load() async {
        do { devices = try await model.api.get("/v1/devices"); message = nil }
        catch { message = "Couldn't load devices: \(error.localizedDescription)" }
    }

    private func newCode() async {
        do { code = try await model.api.post("/v1/invites/device") } catch { message = error.localizedDescription }
    }

    private func revoke(_ d: DeviceItem) async {
        do {
            let _: OK = try await model.api.post("/v1/devices/\(d.id)/revoke")
            await load()
        } catch { message = error.localizedDescription }
    }
}
