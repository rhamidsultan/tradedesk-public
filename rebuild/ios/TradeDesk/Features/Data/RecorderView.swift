import SwiftUI

/// More › Market data › Recording: is today's price stream being recorded, the daily log, and what to record.
struct RecorderView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true
    @State private var info: RecorderInfo?
    @State private var edit: RecorderInfo.Settings?
    @State private var message: String?

    var body: some View {
        Form {
            if let s = info?.status {
                Section("Now") {
                    Label(stateText(s), systemImage: stateIcon(s)).foregroundStyle(stateColor(s)).font(.headline)
                    if let t = s.lastTick { LabeledContent("Last price", value: Fmt.dayTime(t, inIST: showIST)) }
                    if let n = s.instruments { LabeledContent("Instruments", value: "\(n)") }
                    if let n = s.reconnects, n > 0 { LabeledContent("Reconnects today", value: "\(n)") }
                    ForEach(s.gaps ?? [], id: \.self) { g in Label(g, systemImage: "bolt.horizontal").font(.caption) }
                }
            }
            if let runs = info?.runs, !runs.isEmpty {
                Section("Daily log") {
                    ForEach(runs) { r in
                        VStack(alignment: .leading, spacing: 2) {
                            Label(Fmt.marketDay(r.day), systemImage: r.status == "ok" ? "checkmark.circle.fill" : levelIcon(r.status))
                                .foregroundStyle(r.status == "ok" ? Color.tdGood : levelColor(r.status))
                            Text(r.reason.isEmpty ? "Recorded in full" : r.reason).font(.caption).foregroundStyle(.secondary)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
            if model.user?.role == "owner", let e = edit {
                Section {
                    ForEach(["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"], id: \.self) { u in
                        Toggle(u, isOn: Binding(get: { e.underlyings.contains(u) }, set: { on in
                            var s = e
                            if on { s.underlyings.append(u) } else { s.underlyings.removeAll { $0 == u } }
                            edit = s
                        }))
                    }
                    Stepper("Expiries: \(e.expiries)", value: Binding(get: { e.expiries }, set: { edit?.expiries = $0 }), in: 1...4)
                    Stepper("Strikes each side: \(e.strikesEachSide)",
                            value: Binding(get: { e.strikesEachSide }, set: { edit?.strikesEachSide = $0 }), in: 0...60, step: 5)
                    Toggle("Record 1-second bars", isOn: Binding(get: { e.recordSeconds }, set: { edit?.recordSeconds = $0 }))
                    Stepper("Keep 1-second days: \(e.keepSecondsDays)",
                            value: Binding(get: { e.keepSecondsDays }, set: { edit?.keepSecondsDays = $0 }), in: 1...60)
                    Button("Save") { Task { await save() } }
                        .disabled(e == info?.settings || e.underlyings.isEmpty)
                } header: {
                    Text("What to record")
                } footer: {
                    Text("Changes apply from the next trading day. Zerodha keeps no history finer than one minute, so 1-second days that are not recorded cannot be fetched later.")
                }
            }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Recording")
        .task { await load() }
        .refreshable { await load() }
    }

    private func stateText(_ s: RecorderInfo.Status) -> String {
        if s.stale == true { return "Recording, but no prices are arriving" }
        switch s.state {
        case "recording": return "Recording today's prices"
        case "waiting": return s.note ?? "Waiting"
        case "finished": return "Finished for today"
        default: return s.note.map { "Idle: \($0)" } ?? "Not running"
        }
    }
    private func stateIcon(_ s: RecorderInfo.Status) -> String {
        s.stale == true ? "wifi.exclamationmark" : s.state == "recording" ? "record.circle" : s.state == "waiting" ? "hourglass" : "pause.circle"
    }
    private func stateColor(_ s: RecorderInfo.Status) -> Color {
        s.stale == true ? .tdWarn : s.state == "recording" ? .tdGood : s.state == "waiting" ? .tdWarn : .secondary
    }

    private func load() async {
        do { info = try await model.api.get("/v1/recorder"); edit = info?.settings; message = nil }
        catch { message = error.localizedDescription }
    }

    private func save() async {
        guard let e = edit else { return }
        do {
            let s: RecorderInfo.Settings = try await model.api.put("/v1/recorder/settings", e)
            edit = s
            message = "Saved. Applies from the next trading day."
            await load()
        } catch { message = error.localizedDescription }
    }
}
