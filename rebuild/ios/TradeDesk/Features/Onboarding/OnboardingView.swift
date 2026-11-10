import SwiftUI

/// First run: server address + invite code, then a Secure Enclave key protected by Face ID.
struct OnboardingView: View {
    @Environment(AppModel.self) private var model
    @State private var server = ""
    @State private var invite = ""
    @State private var deviceName = "iPhone"
    @FocusState private var focus: Field?
    enum Field { case server, invite, name }

    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                VStack(spacing: 10) {
                    Image(systemName: "chart.line.uptrend.xyaxis")
                        .font(.system(size: 44, weight: .semibold))
                        .foregroundStyle(.tint)
                        .padding(22)
                        .glassEffect(.regular.interactive(), in: .circle)
                    Text("TradeDesk").font(.largeTitle.bold())
                    Text("Index options and stocks, every trade with a stop loss.")
                        .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
                .padding(.top, 40)

                GlassCard {
                    CardHeader(title: "Connect this iPhone", systemImage: "iphone.gen3")
                    field("Server address", text: $server, prompt: "trade.example.com", field: .server)
                        .keyboardType(.URL).textContentType(.URL)
                    field("Invite code", text: $invite, prompt: "K7QM-2XHD-9RTB", field: .invite, caps: .characters)
                    field("Name for this device", text: $deviceName, prompt: "Hamid's iPhone", field: .name)
                    Text("Get an invite code on the server with `python -m tdserver.cli create-owner-invite`.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                GlassCard {
                    Label("Your sign-in key is made inside this iPhone's Secure Enclave and unlocked with Face ID. "
                          + "It never leaves the phone, and there is no password to steal.",
                          systemImage: "faceid")
                        .font(.footnote)
                }

                if let e = model.lastError {
                    Label(e, systemImage: "exclamationmark.triangle.fill").foregroundStyle(Color.tdBad).font(.footnote)
                }

                Button {
                    focus = nil
                    Task { await model.enroll(server: server, invite: invite, deviceName: deviceName) }
                } label: {
                    HStack {
                        if model.busy { ProgressView() }
                        Text("Create key with Face ID").frame(maxWidth: .infinity)
                    }
                    .padding(.vertical, 6)
                }
                .buttonStyle(.glassProminent)
                .controlSize(.large)
                .disabled(model.busy || server.isEmpty || invite.count < 12 || deviceName.isEmpty)
            }
            .padding()
        }
        .scrollDismissesKeyboard(.interactively)
        .background(AppBackground())
        .onAppear { if let s = model.serverURL { server = s.absoluteString } }
    }

    private func field(_ label: String, text: Binding<String>, prompt: String, field: Field,
                       caps: TextInputAutocapitalization = .never) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            TextField(label, text: text, prompt: Text(prompt))
                .font(.body.monospaced())
                .autocorrectionDisabled()
                .textInputAutocapitalization(caps)
                .focused($focus, equals: field)
                .padding(12)
                .background(.background.opacity(0.6), in: .rect(cornerRadius: 12))
        }
    }
}

/// Signed out or session ended: one tap, Face ID.
struct LockView: View {
    @Environment(AppModel.self) private var model
    @State private var confirmReset = false

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: "lock.shield")
                .font(.system(size: 52, weight: .semibold))
                .padding(26)
                .glassEffect(.regular, in: .circle)
            Text("TradeDesk is locked").font(.title2.bold())
            if let name = model.user?.name { Text("Signed in as \(name)").foregroundStyle(.secondary) }
            if let e = model.lastError {
                Label(e, systemImage: "exclamationmark.triangle.fill").foregroundStyle(Color.tdBad).font(.footnote)
            }
            Button {
                Task { await model.signIn() }
            } label: {
                Label("Unlock with Face ID", systemImage: "faceid").frame(maxWidth: .infinity).padding(.vertical, 6)
            }
            .buttonStyle(.glassProminent)
            .controlSize(.large)
            .disabled(model.busy)
            Spacer()
            Button("Use a different server or invite") { confirmReset = true }
                .font(.footnote)
                .confirmationDialog("Start again on this iPhone?", isPresented: $confirmReset, titleVisibility: .visible) {
                    Button("Delete sign-in key", role: .destructive) { model.resetDevice() }
                } message: {
                    Text("Removes this iPhone's sign-in key. You will need a new invite code from the server.")
                }
        }
        .padding(24)
        .background(AppBackground())
        .task { if model.lastError == nil { await model.signIn() } }
    }
}
