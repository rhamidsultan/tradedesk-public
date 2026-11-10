import SwiftUI

/// The deploy sheet: exactly what the session will run (the saved version, a preset, lots), its stop and
/// limits, loss per trade at the stop, margin estimate, the kill rules and gate fixed for its whole life, and
/// when it starts. Amounts always show here, whatever the hide-amounts setting: this is a risk decision.
struct DeploySheet: View {
    @Environment(AppModel.self) private var model
    let strategyID: Int
    var done: (Int?) -> Void
    @State private var presets: [JSONValue] = []
    @State private var presetID: Int? = nil
    @State private var lots = 0              // 0 = as saved
    @State private var pv: JSONValue?
    @State private var error: String?
    @State private var startError: String?
    @State private var presetError: String?
    @State private var sending = false
    @State private var previewTask: Task<Void, Never>?

    /// The preview on screen is for exactly the settings chosen now (never a stale one while a new one loads).
    private var previewCurrent: Bool {
        guard let pv else { return false }
        return pv["asked"]?["lots"]?.number.map { Int($0) } == (lots > 0 ? lots : nil)
            && pv["asked"]?["preset_id"]?.number.map { Int($0) } == presetID
    }

    private var maxLots: Int { Int(pv?["max_lots"]?.number ?? 50) }

    var body: some View {
        Form {
            Section {
                Picker("Settings", selection: $presetID) {
                    Text("As saved").tag(Int?.none)
                    ForEach(Array(presets.enumerated()), id: \.offset) { _, p in
                        Text(p["name"]?.string ?? "").tag(Int?.some(Int(p["id"]?.number ?? 0)))
                    }
                }
                Stepper(lots == 0 ? "Lots: \(savedLots) (as saved)" : "Lots: \(lots)", value: $lots, in: 0...max(1, min(50, maxLots)))
                    .frame(minHeight: 44)
                if let presetError {
                    HStack {
                        Label(presetError, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Color.tdWarn)
                        Spacer()
                        Button("Retry") { Task { await loadPresets() } }
                    }
                }
            } footer: { Text("Max lots for this strategy: \(maxLots).") }
            if let error {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn)
                    Button("Retry") { Task { await loadPreview() } }
                }
            }
            if let pv {
                ForEach((pv["blockers"]?.array ?? []).compactMap(\.string), id: \.self) { b in
                    Label(b, systemImage: "xmark.octagon").foregroundStyle(Color.tdBad)
                }
                Section("What runs") {
                    LabeledContent("Strategy", value: "\(pv["strategy"]?["name"]?.string ?? "") · v\(Int(pv["strategy"]?["version"]?.number ?? 0))")
                    ForEach(Array((pv["legs"]?.array ?? []).enumerated()), id: \.offset) { _, l in
                        LabeledContent("\(l["side"]?.string == "SELL" ? "Sell" : "Buy") \(l["type"]?.string ?? "")",
                                       value: "\(strikeText(l["strike"])) · \(lotsText(l["lots"])) · \(humanize(l["expiry"]?.string ?? ""))")
                    }
                    LabeledContent("Starts", value: pv["starts"]?["text"]?.string ?? "—")
                }
                Section("Risk") {
                    LabeledContent("Stop loss", value: stopText(pv["stop_loss"]))
                    LabeledContent("Max loss a day", value: money(pv["max_loss_per_day"]?.number))
                    let r = pv["risk"]
                    LabeledContent("Loss per trade at the stop") {
                        Text(money(r?["loss_per_trade"]?.number)).foregroundStyle(Color.tdBad)
                    }
                    if let w = r?["app_down_worst"]?.number {
                        LabeledContent("Worst case, both stops past trigger") { Text(money(w)).foregroundStyle(Color.tdBad) }
                    }
                    if let m = r?["margin_per_lot"]?.number {
                        let n = Double(lots > 0 ? lots : max(1, savedLotsNumber))
                        LabeledContent("Margin estimate", value: "≈ \(money(m * n)) for \(Int(n)) lot\(n == 1 ? "" : "s")")
                    }
                    LabeledContent("Capital", value: money(r?["capital"]?.number))
                }
                .monospacedDigit()
                Section {
                    let k = pv["rules"]?["kill"]
                    let g = pv["rules"]?["gate"]
                    LabeledContent("Pause if the total falls below", value: "−" + money(k?["cum_net_below"]?.number))
                    LabeledContent("Pause if a day loses more than", value: money(k?["day_worse_than"]?.number))
                    LabeledContent("Pause if median stop slippage", value: "> ₹\((k?["median_slip_above"]?.number ?? 0).formatted()) a unit")
                    LabeledContent("Gate to the next stage",
                                   value: "\(Int(g?["cycles"]?.number ?? 0)) cycles · \(Int(g?["weeks"]?.number ?? 0)) weeks · \(Int(g?["stop_events"]?.number ?? 0)) stops")
                } header: { Text("Rules, fixed for this session") } footer: {
                    Text("Set now and never changed, so a bad run can't move its own goalposts.")
                }
                .monospacedDigit()
                Section {
                    ForEach(Array((pv["modes"]?.array ?? []).enumerated()), id: \.offset) { _, m in
                        Label {
                            VStack(alignment: .leading) {
                                Text(m["mode"]?.string == "paper" ? "Paper" : "Shadow").font(.subheadline.weight(.semibold))
                                Text(m["text"]?.string ?? "").font(.caption).foregroundStyle(.secondary)
                            }
                        } icon: {
                            Image(systemName: m["available"]?.bool == true ? "checkmark.circle.fill" : "lock")
                                .foregroundStyle(m["available"]?.bool == true ? Color.tdPaper : .secondary)
                        }
                    }
                }
            } else if error == nil {
                ProgressView()
            }
        }
        .safeAreaInset(edge: .bottom) { startBar }
        .navigationTitle("Deploy to paper")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await loadPresets()
            presetID = presets.first { $0["is_default"]?.bool == true }.map { Int($0["id"]?.number ?? 0) }
            schedulePreview()
        }
        .onChange(of: presetID) { schedulePreview() }
        .onChange(of: lots) { schedulePreview() }
    }

    private var savedLotsNumber: Int {
        (pv?["legs"]?.array ?? []).compactMap { $0["lots"]?.number }.map { Int($0) }.max() ?? 0
    }
    private var savedLots: String { savedLotsNumber > 0 ? "\(savedLotsNumber)" : "from risk" }

    private var startBar: some View {
        VStack(spacing: 6) {
            if let startError {
                Label(startError, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(Color.tdBad)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Button {
                Task { await start() }
            } label: {
                HStack {
                    if sending || (pv != nil && !previewCurrent) { ProgressView() }
                    Text(pv?["max_loss_per_day"]?.number.map { "Start paper · max loss \(Fmt.rupees($0)) a day" } ?? "Start paper session")
                        .font(.headline)
                }
                .frame(maxWidth: .infinity, minHeight: 50)
            }
            .buttonStyle(.borderedProminent).tint(Color.tdPaper)
            .disabled(sending || !previewCurrent || !(pv?["blockers"]?.array ?? []).isEmpty)
            Text("PAPER · no real orders").font(.caption2).foregroundStyle(.secondary)
        }
        .padding(.horizontal).padding(.top, 8)
        .background(.bar)
    }

    private func loadPresets() async {
        do {
            presets = try await model.api.raw("GET", "/v1/strategies/\(strategyID)/presets").array ?? []
            presetError = nil
        } catch { presetError = "Couldn't load presets." }
    }

    private func schedulePreview() {
        previewTask?.cancel()
        previewTask = Task {
            try? await Task.sleep(for: .milliseconds(250))
            if Task.isCancelled { return }
            await loadPreview()
        }
    }

    private func money(_ v: Double?) -> String { v.map { Fmt.rupees($0) } ?? "—" }

    private func strikeText(_ v: JSONValue?) -> String {
        let n = v?["value"]?.number ?? 0
        if v?["by"]?.string == "premium" { return "premium near ₹\(n.formatted())" }
        return n == 0 ? "ATM" : "ATM \(n > 0 ? "+" : "−") \(Int(abs(n))) strike\(abs(n) == 1 ? "" : "s")"
    }

    private func lotsText(_ v: JSONValue?) -> String {
        if let n = v?.number { return "\(Int(n)) lot\(n == 1 ? "" : "s")" }
        return "lots from risk"
    }

    private func stopText(_ v: JSONValue?) -> String {
        guard let v, let val = v["value"]?.number else { return "—" }
        let n = val.formatted(.number.precision(.fractionLength(0...2)))
        switch (v["on"]?.string, v["type"]?.string) {
        case ("underlying"?, _): return "Index \(n) pts (app-held)"
        case (_, "multiple"?): return "\(n)× premium"
        case (_, "percent"?): return "\(n)% of premium"
        default: return "\(n) pts of premium"
        }
    }

    private func loadPreview() async {
        var q = "/v1/paper/preview?strategy_id=\(strategyID)"
        if let presetID { q += "&preset_id=\(presetID)" }
        if lots > 0 { q += "&lots=\(lots)" }
        do {
            let fresh = try await model.api.raw("GET", q)
            if Task.isCancelled { return }
            pv = fresh; error = nil; startError = nil
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }

    private func start() async {
        sending = true; defer { sending = false }
        var body: [String: JSONValue] = ["strategy_id": .number(Double(strategyID))]
        if let presetID { body["preset_id"] = .number(Double(presetID)) }
        if lots > 0 { body["lots"] = .number(Double(lots)) }
        do {
            let r = try await model.api.raw("POST", "/v1/paper/sessions", body: JSONValue.object(body))
            done(r["id"]?.number.map { Int($0) })
        } catch { startError = error.localizedDescription }
    }
}
