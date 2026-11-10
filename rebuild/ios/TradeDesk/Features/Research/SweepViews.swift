import SwiftUI

/// Settings people sweep most, as paths into the strategy (any other path can be typed).
let SWEEP_PATHS: [(String, String)] = [
    ("risk.stop_loss.value", "Stop loss"), ("risk.target.value", "Target"), ("risk.max_trades_per_day", "Max trades a day"),
    ("legs.0.strike.value", "Leg 1 strike or premium"), ("legs.1.strike.value", "Leg 2 strike or premium"),
    ("risk.trailing.value", "Trail by"), ("risk.position_stop", "Whole-trade stop ₹"),
]

/// Find best settings: try up to 60 combinations on the earlier part of the dates, check the best ones on the later
/// part (and again with conservative fills), drop settings whose neighbours lose, optionally walk forward.
struct SweepFormView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let template: String?
    let specFromRun: Int?
    @State private var rows: [(path: String, values: String)] = [("risk.stop_loss.value", "1.5, 2, 2.5, 3")]
    @State private var start = Calendar.current.date(byAdding: .year, value: -2, to: .now) ?? .now
    @State private var split = Calendar.current.date(byAdding: .month, value: -8, to: .now) ?? .now
    @State private var end = Date.now
    @State private var folds = 0
    @State private var spec: JSONValue?
    @State private var message: String?
    @State private var sending = false

    private var combos: Int { rows.map { parse($0.values).count }.reduce(1) { $0 * max(1, $1) } }

    var body: some View {
        Form {
            Section {
                ForEach(rows.indices, id: \.self) { i in
                    VStack(alignment: .leading) {
                        Picker("Setting", selection: Binding(get: { rows[i].path }, set: { rows[i].path = $0 })) {
                            ForEach(SWEEP_PATHS, id: \.0) { Text($0.1).tag($0.0) }
                        }
                        TextField("Values, comma separated", text: Binding(get: { rows[i].values }, set: { rows[i].values = $0 }))
                            .keyboardType(.numbersAndPunctuation).font(.body.monospacedDigit())
                    }
                }
                if rows.count < 2 { Button("Add a second setting", systemImage: "plus") { rows.append(("risk.max_trades_per_day", "1, 2")) } }
                else { Button("Remove the second setting", role: .destructive) { rows.removeLast() } }
                LabeledContent("Combinations", value: "\(combos) of 60").foregroundStyle(combos > 60 ? Color.tdBad : .primary)
            } header: { Text("Settings to try") }
            Section {
                DatePicker("From", selection: $start, in: ...Date.now, displayedComponents: .date)
                DatePicker("Pick on data to", selection: $split, in: start...end, displayedComponents: .date)
                DatePicker("Check on data to", selection: $end, in: split...Date.now, displayedComponents: .date)
                Stepper("Walk-forward periods: \(folds)", value: $folds, in: 0...4)
            } header: { Text("Dates") } footer: {
                Text("The best settings are picked on the first part only, then checked on the part they never saw. That check is what to trust.")
            }
            Section {
                Button {
                    Task { await send() }
                } label: { HStack { Text("Start the sweep"); if sending { Spacer(); ProgressView() } } }
                .disabled(sending || combos > 60 || rows.contains { parse($0.values).isEmpty })
            } footer: { Text("Sweeps run on the server after the close on trading days. Each combination is a full backtest with honest fills.") }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Find best settings")
        .task { await loadSpec() }
    }

    private func parse(_ s: String) -> [Double] {
        s.split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
    }

    private func loadSpec() async {
        guard let id = specFromRun else { return }
        spec = try? await model.api.raw("GET", "/v1/research/backtests/\(id)")["spec"]
    }

    private func send() async {
        sending = true; defer { sending = false }
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = Fmt.ist
        var grid: [String: JSONValue] = [:]
        for r in rows { grid[r.path] = .array(parse(r.values).map(JSONValue.number)) }
        var body: [String: JSONValue] = ["start": .string(f.string(from: start)), "split": .string(f.string(from: split)),
                                         "end": .string(f.string(from: end)), "folds": .number(Double(folds)),
                                         "grid": .object(grid), "fill_model": .string("honest")]
        if let template { body["template"] = .string(template) } else if let spec { body["spec"] = spec }
        do {
            _ = try await model.api.raw("POST", "/v1/research/sweeps", body: JSONValue.object(body))
            dismiss()
        } catch { message = error.localizedDescription }
    }
}

struct SweepResultView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    let runID: Int
    @State private var d: JSONValue?
    @State private var message: String?

    var body: some View {
        List {
            if let d {
                if d["status"]?.string != "done" {
                    Label(d["status"]?.string == "running" ? "Running · \(Int(d["progress"]?.number ?? 0))%" : (d["note"]?.string ?? "Waiting"),
                          systemImage: "hourglass")
                } else if let sw = d["sweep"] {
                    Section {
                        if let best = sw["best"]?.object {
                            Label(best.map { "\(label($0.key)) \(show($0.value))" }.joined(separator: " · "), systemImage: "star.fill")
                                .font(.headline).foregroundStyle(Color.tdGood)
                        } else {
                            Label("No setting passed", systemImage: "xmark.octagon").font(.headline).foregroundStyle(Color.tdBad)
                        }
                        Text(sw["best_reason"]?.string ?? "").font(.footnote)
                        if let luck = sw["luck_warning"]?.string { Label(luck, systemImage: "dice").font(.caption).foregroundStyle(Color.tdWarn) }
                        Text("Pick on \(period(sw["periods.in_sample"])) · check on \(period(sw["periods.out_of_sample"]))").font(.caption)
                    }
                    if let grid = d["params.grid"]?.object, grid.count == 2 { heatmap(sw, grid) }
                    Section("Ranked (steadiest first)") {
                        let ranked = ranked(sw)
                        ForEach(Array(ranked.enumerated()), id: \.offset) { i, r in row(i, r) }
                    }
                    if let wf = sw["walk_forward"]?.array, !wf.isEmpty {
                        Section("Walk-forward") {
                            ForEach(Array(wf.enumerated()), id: \.offset) { _, w in
                                LabeledContent("Test \(period(w["test"]))") {
                                    Text(money(w["test_result.net_pnl"]?.number)).foregroundStyle((w["test_result.net_pnl"]?.number ?? 0) >= 0 ? Color.tdGood : Color.tdBad)
                                }
                                .monospacedDigit()
                            }
                        }
                    }
                }
            } else { ProgressView() }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Sweep")
        .task { await load() }
        .refreshable { await load() }
    }

    private func ranked(_ sw: JSONValue) -> [JSONValue] {
        let rows = sw["rows"]?.array ?? []
        let order = (sw["ranked"]?.array ?? [])
        return order.compactMap { p in rows.first { $0["params"] == p } }
    }

    @ViewBuilder private func row(_ i: Int, _ r: JSONValue) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("\(i + 1). " + (r["params"]?.object ?? [:]).map { "\(label($0.key)) \(show($0.value))" }.joined(separator: " · "))
                    .font(.subheadline.weight(.semibold))
                if r["fragile"]?.bool == true { StatusPill(text: "Fragile", systemImage: "exclamationmark.triangle", color: .tdWarn) }
            }
            HStack(spacing: 10) {
                Text("Picked on: \(money(r["in_sample.net_pnl"]?.number))")
                if let o = r["out_of_sample.net_pnl"]?.number { Text("Checked: \(money(o))").foregroundStyle(o >= 0 ? Color.tdGood : Color.tdBad) }
                if let c = r["out_of_sample_conservative.net_pnl"]?.number { Text("Conservative: \(money(c))") }
            }
            .font(.caption.monospacedDigit())
            Button("Backtest this setting") { Task { await backtest(r["params"]) } }.font(.caption).buttonStyle(.borderless)
        }
        .accessibilityElement(children: .combine)
    }

    private func heatmap(_ sw: JSONValue, _ grid: [String: JSONValue]) -> some View {
        let keys = grid.keys.sorted()
        let xs = grid[keys[0]]?.array ?? [], ys = grid[keys[1]]?.array ?? []
        let rows = sw["rows"]?.array ?? []
        let nets = rows.compactMap { $0["in_sample.net_pnl"]?.number }
        let maxAbs = max(1, nets.map(abs).max() ?? 1)
        return Section("\(label(keys[0])) × \(label(keys[1])) (picked-on net)") {
            Grid(horizontalSpacing: 3, verticalSpacing: 3) {
                GridRow { Text(""); ForEach(xs.indices, id: \.self) { Text(show(xs[$0])).font(.caption2) } }
                ForEach(ys.indices, id: \.self) { yi in
                    GridRow {
                        Text(show(ys[yi])).font(.caption2)
                        ForEach(xs.indices, id: \.self) { xi in
                            let net = rows.first { $0["params"] == .object([keys[0]: xs[xi], keys[1]: ys[yi]]) }?["in_sample.net_pnl"]?.number
                            Text(net.map { $0 >= 0 ? "+" : "−" } ?? "·").font(.caption.weight(.bold))
                                .frame(maxWidth: .infinity, minHeight: 30)
                                .background(((net ?? 0) >= 0 ? Color.tdGood : Color.tdBad).opacity(net == nil ? 0.05 : 0.2 + 0.6 * abs(net ?? 0) / maxAbs),
                                            in: .rect(cornerRadius: 4))
                                .accessibilityLabel("\(show(xs[xi])), \(show(ys[yi])): \(money(net))")
                        }
                    }
                }
            }
        }
    }

    private func label(_ path: String) -> String { SWEEP_PATHS.first { $0.0 == path }?.1 ?? humanize(path.components(separatedBy: ".").last ?? path) }
    private func show(_ v: JSONValue?) -> String { v?.number.map { $0.formatted() } ?? v?.string ?? "—" }
    private func period(_ v: JSONValue?) -> String {
        let a = v?.array ?? []
        return a.count == 2 ? "\(Fmt.marketDay(a[0].string)) – \(Fmt.marketDay(a[1].string))" : "—"
    }
    private func money(_ v: Double?) -> String { v.map { Fmt.rupees($0, hidden: hide, signed: true) } ?? "—" }

    private func load() async {
        do {
            d = try await model.api.raw("GET", "/v1/research/backtests/\(runID)")
            if d?["status"]?.string == "queued" || d?["status"]?.string == "running" {
                try? await Task.sleep(for: .seconds(5)); await load()
            }
        } catch { message = error.localizedDescription }
    }

    private func backtest(_ params: JSONValue?) async {
        guard let d, let spec = d["spec"], let p = params?.object else { return }
        let body: JSONValue = .object(["spec": spec, "overrides": .object(p), "start": d["start"] ?? .null, "end": d["end"] ?? .null,
                                       "fill_model": .string("honest"), "name": .string("From sweep \(runID)")])
        do { _ = try await model.api.raw("POST", "/v1/research/backtests", body: body); message = "Queued as a backtest." }
        catch { message = error.localizedDescription }
    }
}
