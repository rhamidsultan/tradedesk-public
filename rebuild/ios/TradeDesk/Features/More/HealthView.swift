import SwiftUI

/// More › Server health: a light per check with the reason under it. Amber after one failed check, red after two.
struct HealthView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true
    @State private var info: HealthInfo?
    @State private var error: String?

    var body: some View {
        List {
            if let error { Label(error, systemImage: "wifi.slash").foregroundStyle(Color.tdWarn) }
            if let h = info {
                Section {
                    ForEach(h.checks) { c in
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: icon(c.light)).foregroundStyle(color(c.light)).font(.title3)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.name).font(.headline)
                                Text(c.reason).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("\(c.name), \(word(c.light)). \(c.reason)")
                    }
                } footer: {
                    Text(h.checkedAt.map { "Checked \(Fmt.dayTime($0, inIST: showIST)) · every minute" } ?? "Not checked yet")
                }
            } else if error == nil {
                ProgressView()
            }
        }
        .navigationTitle("Server health")
        .task { await load() }
        .refreshable { await load() }
    }

    private func icon(_ l: String) -> String {
        switch l { case "green": "checkmark.circle.fill"; case "amber": "exclamationmark.circle.fill"
        case "red": "xmark.octagon.fill"; default: "minus.circle" }
    }
    private func color(_ l: String) -> Color {
        switch l { case "green": .tdGood; case "amber": .tdWarn; case "red": .tdBad; default: .secondary }
    }
    private func word(_ l: String) -> String {
        switch l { case "green": "fine"; case "amber": "needs a look"; case "red": "problem"; default: "not checked now" }
    }

    private func load() async {
        do { info = try await model.api.get("/v1/health/detail"); error = nil }
        catch { self.error = error.localizedDescription }
    }
}

/// About › Server version: what runs, whether merged code is waiting, and Update now (owner).
struct ServerVersionSection: View {
    @Environment(AppModel.self) private var model
    @State private var v: VersionInfo?
    @State private var confirm = false
    @State private var message: String?

    var body: some View {
        Section {
            if let v {
                LabeledContent("Running", value: "\(v.version) · \(v.commit)")
                if let a = v.available, a.behind > 0 {
                    Label("\(a.behind) update\(a.behind == 1 ? "" : "s") waiting: \(a.subject)", systemImage: "arrow.down.circle")
                        .foregroundStyle(.tint)
                } else if v.available != nil {
                    Label("Up to date", systemImage: "checkmark.circle").foregroundStyle(Color.tdGood)
                }
                if let u = v.lastUpdate {
                    Label("Last update: \(humanize(u.state)) — \(u.detail)", systemImage: u.state == "done" ? "checkmark" :
                            u.state == "running" ? "arrow.triangle.2.circlepath" : "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(u.state == "done" ? Color.secondary : u.state == "running" ? Color.tdPaper : Color.tdWarn)
                }
                if model.user?.role == "owner" {
                    Button("Update now", systemImage: "arrow.down.circle") { confirm = true }
                        .disabled(v.requested || v.lastUpdate?.state == "running")
                        .confirmationDialog("Update the server now?", isPresented: $confirm, titleVisibility: .visible) {
                            Button("Update now") { Task { await update() } }
                        } message: {
                            Text("The server installs the merged code from GitHub, restarts for a few seconds and checks itself. If it does not start, the old version comes back."
                                 + (model.status?.market.state == "open" || model.status?.market.state == "fo_only"
                                    ? " The market is open: the recorder and paper sessions pause for a few seconds." : ""))
                        }
                }
            } else if message == nil {
                ProgressView()
            }
            if let message { Text(message).font(.footnote) }
        } header: { Text("Server version") }
        .task { await load() }
    }

    private func load() async {
        do { v = try await model.api.get("/v1/system/version") } catch { message = error.localizedDescription }
    }

    private func update() async {
        struct R: Decodable { let ok: Bool; let note: String }
        do {
            let r: R = try await model.api.post("/v1/system/update")
            message = r.note
            for _ in 0..<40 {                               // follow it for up to 2 minutes
                try? await Task.sleep(for: .seconds(3))
                await load()
                if let s = v?.lastUpdate?.state, s != "running", v?.requested == false { break }
            }
        } catch { message = error.localizedDescription }
    }
}
