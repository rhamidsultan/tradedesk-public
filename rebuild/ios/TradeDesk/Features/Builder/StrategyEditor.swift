import SwiftUI

/// The no-code builder: Instrument, Entry, Legs, Risk, Exit, Schedule, Costs, then the risk step and Save.
/// Every change is kept as the server-side draft (so it can be continued later), and the server's plain-language
/// checks say what to fix before it can be saved or backtested.
struct StrategyEditor: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let strategyID: Int
    @State private var spec: JSONValue = .object([:])
    @State private var loaded = false
    @State private var problems: [Problem] = []
    @State private var warnings: [Problem] = []
    @State private var saveTask: Task<Void, Never>?
    @State private var error: String?

    struct Problem: Hashable { let where_: String; let text: String }

    var body: some View {
        Form {
            if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            Section {
                TextField("Name", text: bind.text("name"))
                if problems.isEmpty {
                    Label("Ready to save and backtest", systemImage: "checkmark.seal.fill").foregroundStyle(Color.tdGood)
                } else {
                    Label("Fix before saving or backtesting", systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.tdBad).font(.headline)
                    ForEach(problems, id: \.self) { p in
                        VStack(alignment: .leading) {
                            Text(p.where_).font(.caption.weight(.semibold))
                            Text(p.text).font(.subheadline)
                        }
                    }
                }
                ForEach(warnings, id: \.self) { w in
                    Label("\(w.where_): \(w.text)", systemImage: "info.circle").font(.caption).foregroundStyle(Color.tdWarn)
                }
            }
            if loaded {
                Section("Steps") {
                    NavigationLink { InstrumentStep(bind: bind) } label: { step("Instrument", "chart.bar", summaryInstrument) }
                    if spec["kind"]?.string != "python" {
                        NavigationLink { EntryStep(bind: bind) } label: { step("Entry", "arrow.right.circle", summaryEntry) }
                        NavigationLink { LegsStep(bind: bind) } label: { step("Legs", "square.stack.3d.up", summaryLegs) }
                    } else {
                        NavigationLink { ParamsStep(bind: bind) } label: { step("Settings", "slider.horizontal.3", "Python strategy settings") }
                    }
                    NavigationLink { RiskStep(bind: bind) } label: { step("Risk", "shield.lefthalf.filled", summaryRisk) }
                    NavigationLink { ScheduleStep(bind: bind) } label: { step("Exit and schedule", "clock", summarySchedule) }
                    NavigationLink { CostsStep(bind: bind) } label: { step("Costs and fills", "indianrupeesign.circle", summaryCosts) }
                }
                Section {
                    NavigationLink {
                        RiskReview(strategyID: strategyID, spec: spec, ready: problems.isEmpty) { dismiss() }
                    } label: {
                        Label("Check the risk and save", systemImage: "checkmark.shield").font(.headline)
                    }
                    .disabled(!problems.isEmpty)
                } footer: {
                    Text("Changes are kept as a draft as you go. Saving makes a new version; older versions stay.")
                }
            } else {
                ProgressView()
            }
        }
        .navigationTitle(spec["name"]?.string ?? "Strategy")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .onChange(of: spec) { _, _ in if loaded { scheduleDraftSave() } }
    }

    private var bind: SpecBinding { SpecBinding(spec: $spec) }

    private func step(_ title: String, _ icon: String, _ summary: String) -> some View {
        let key = ["Exit and schedule": "Schedule", "Costs and fills": "Costs", "Settings": "Strategy"][title] ?? title
        let bad = problems.contains { $0.where_.hasPrefix(key) }
        return HStack {
            Label(title, systemImage: icon)
            Spacer()
            if bad { Image(systemName: "exclamationmark.circle.fill").foregroundStyle(Color.tdBad).accessibilityLabel("needs fixing") }
        }
        .badge(Text(summary).font(.caption))
    }

    private var summaryInstrument: String {
        "\(spec["instrument.underlying"]?.string ?? "—") · \(spec["instrument.timeframe"]?.string ?? "—")"
    }
    private var summaryEntry: String { "\(spec["entry.any_of"]?.array?.count ?? 0) rule(s)" }
    private var summaryLegs: String { "\(spec["legs"]?.array?.count ?? 0) leg(s)" }
    private var summaryRisk: String {
        guard let v = spec["risk.stop_loss.value"]?.number else { return "No stop loss" }
        return "Stop \(v.formatted()) \(spec["risk.stop_loss.type"]?.string ?? "")"
    }
    private var summarySchedule: String {
        "\(spec["schedule.start"]?.string ?? "09:20")–\(spec["schedule.exit_by"]?.string ?? "15:15")"
            + (spec["schedule.hold_overnight"]?.bool == true ? " · overnight" : "")
    }
    private var summaryCosts: String { spec["costs.fill_model"]?.string == "classic" ? "Classic fills" : "Honest fills" }

    private func load() async {
        do {
            let d = try await model.api.raw("GET", "/v1/strategies/\(strategyID)")
            spec = d["spec"] ?? .object([:])
            apply(d)
            loaded = true
        } catch { self.error = error.localizedDescription }
    }

    private func apply(_ d: JSONValue) {
        problems = (d["problems"]?.array ?? []).map { Problem(where_: $0["where"]?.string ?? "", text: $0["text"]?.string ?? "") }
        warnings = (d["warnings"]?.array ?? []).map { Problem(where_: $0["where"]?.string ?? "", text: $0["text"]?.string ?? "") }
    }

    private func scheduleDraftSave() {
        saveTask?.cancel()
        let current = spec
        saveTask = Task {
            try? await Task.sleep(for: .milliseconds(600))
            guard !Task.isCancelled else { return }
            struct B: Encodable { let spec: JSONValue }
            do {
                let r = try await model.api.raw("PUT", "/v1/strategies/\(strategyID)/draft", body: B(spec: current))
                apply(r)
                error = nil
            } catch { self.error = "Draft not saved: \(error.localizedDescription)" }
        }
    }
}

/// Bindings into the spec by dotted path.
struct SpecBinding {
    @Binding var spec: JSONValue

    func text(_ path: String, _ fallback: String = "") -> Binding<String> {
        Binding(get: { spec[path]?.string ?? fallback }, set: { spec[path] = .string($0) })
    }
    func number(_ path: String, _ fallback: Double = 0) -> Binding<Double> {
        Binding(get: { spec[path]?.number ?? fallback }, set: { spec[path] = .number($0) })
    }
    func optionalNumber(_ path: String) -> Binding<Double?> {
        Binding(get: { spec[path]?.number }, set: { spec[path] = $0.map(JSONValue.number) })
    }
    /// Optional ₹ amounts: 0 means "not used", so the field is removed rather than sent as 0.
    func positive(_ path: String) -> Binding<Double> {
        Binding(get: { spec[path]?.number ?? 0 }, set: { spec[path] = $0 > 0 ? .number($0) : nil })
    }
    func int(_ path: String, _ fallback: Int = 0) -> Binding<Int> {
        Binding(get: { Int(spec[path]?.number ?? Double(fallback)) }, set: { spec[path] = .number(Double($0)) })
    }
    func bool(_ path: String, _ fallback: Bool = false) -> Binding<Bool> {
        Binding(get: { spec[path]?.bool ?? fallback }, set: { spec[path] = .bool($0) })
    }
    func time(_ path: String, _ fallback: String) -> Binding<Date> {
        Binding(get: {
            let t = spec[path]?.string ?? fallback
            let p = t.split(separator: ":").compactMap { Int($0) }
            var c = DateComponents(); c.hour = p.first ?? 9; c.minute = p.count > 1 ? p[1] : 0
            var cal = Calendar(identifier: .gregorian); cal.timeZone = Fmt.ist
            return cal.date(from: c) ?? .now
        }, set: { d in
            var cal = Calendar(identifier: .gregorian); cal.timeZone = Fmt.ist
            let c = cal.dateComponents([.hour, .minute], from: d)
            spec[path] = .string(String(format: "%02d:%02d", c.hour ?? 9, c.minute ?? 0))
        })
    }
    func remove(_ path: String) { spec[path] = nil }
    func get(_ path: String) -> JSONValue? { spec[path] }
    func set(_ path: String, _ v: JSONValue?) { spec[path] = v }
}

/// "Number" text field that shows nothing for empty and keeps Indian digits readable.
struct NumberField: View {
    let label: String
    @Binding var value: Double
    var unit: String = ""

    var body: some View {
        LabeledContent(label) {
            HStack(spacing: 4) {
                TextField(label, value: $value, format: .number)
                    .keyboardType(.decimalPad).multilineTextAlignment(.trailing).monospacedDigit()
                if !unit.isEmpty { Text(unit).foregroundStyle(.secondary) }
            }
        }
    }
}
