import Charts
import SwiftUI

/// Pick two or three finished runs to compare.
struct ComparePicker: View {
    let runs: [BacktestRunItem]
    @State private var picked: Set<Int> = []

    var body: some View {
        List(runs, selection: $picked) { r in RunRow(run: r).tag(r.id) }
            .environment(\.editMode, .constant(.active))
            .navigationTitle("Compare \(picked.count) of 3")
            .toolbar {
                NavigationLink("Compare") { CompareView(ids: Array(picked).sorted()) }
                    .disabled(picked.count < 2 || picked.count > 3)
            }
    }
}

struct CompareView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    let ids: [Int]
    @State private var data: JSONValue?
    @State private var message: String?

    var body: some View {
        List {
            if let runs = data?["runs"]?.array {
                Section("Equity") {
                    Chart {
                        ForEach(Array(runs.enumerated()), id: \.offset) { _, r in
                            let name = r["run.name"]?.string ?? ""
                            ForEach(Array((r["curve"]?.array ?? []).enumerated()), id: \.offset) { _, p in
                                if let t = Fmt.parseIST(p["date"]?.string ?? ""), let e = p["equity"]?.number {
                                    LineMark(x: .value("Day", t), y: .value("Equity", e)).foregroundStyle(by: .value("Run", name))
                                }
                            }
                        }
                    }
                    .chartYScale(domain: .automatic(includesZero: false))
                    .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if let x = v.as(Double.self) { Text(hide ? "••" : Fmt.rupeesShort(x)) } } } }
                    .frame(height: 220)
                }
                let rows: [(String, String)] = [("net_pnl", "Net after charges"), ("net_with_charges_x1_5", "Charges × 1.5"),
                                                ("max_drawdown", "Max drawdown"), ("score_steady", "Steady score"),
                                                ("profit_factor", "Profit factor"), ("win_rate_pct", "Win rate"), ("trades", "Trades")]
                ForEach(rows, id: \.0) { key, label in
                    Section(label) {
                        ForEach(Array(runs.enumerated()), id: \.offset) { _, r in
                            LabeledContent(r["run.name"]?.string ?? "") { Text(value(key, r["figures.\(key)"]?.number)).monospacedDigit() }
                        }
                    }
                }
            } else { ProgressView() }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Compare")
        .task {
            do { data = try await model.api.raw("GET", "/v1/research/compare?ids=\(ids.map(String.init).joined(separator: ","))") }
            catch { message = error.localizedDescription }
        }
    }

    private func value(_ key: String, _ v: Double?) -> String {
        guard let v else { return "—" }
        switch key {
        case "net_pnl", "net_with_charges_x1_5": return Fmt.rupees(v, hidden: hide, signed: true)
        case "max_drawdown": return Fmt.rupees(-v, hidden: hide)
        case "win_rate_pct": return "\(Int(v))%"
        case "trades": return "\(Int(v))"
        default: return String(format: "%.2f", v)
        }
    }
}

/// Replay a day: the index through the day, each order where it happened, and the P&L as the day goes on.
struct ReplayView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    let template: String?
    var strategyID: Int? = nil
    @State private var day = Calendar.current.date(byAdding: .day, value: -1, to: .now) ?? .now
    @State private var data: JSONValue?
    @State private var minute: Double = 374
    @State private var playing = false
    @State private var message: String?

    var body: some View {
        List {
            Section {
                DatePicker("Day", selection: $day, in: ...Date.now, displayedComponents: .date)
                Button("Load the day") { Task { await load() } }
            }
            if let d = data {
                let idx = (d["index"]?.array ?? [])
                let upto = Int(minute)
                let shown = Array(idx.prefix(upto + 1))
                let nowTs = shown.last?["ts"]?.string ?? ""
                let fills = (d["fills"]?.array ?? []).filter { ($0["ts"]?.string ?? "") <= nowTs + "~" }
                Section {
                    Chart {
                        ForEach(Array(shown.enumerated()), id: \.offset) { _, b in
                            if let t = Fmt.parseIST(b["ts"]?.string ?? ""), let c = b["c"]?.number { LineMark(x: .value("Time", t), y: .value("Index", c)) }
                        }
                        ForEach(Array(fills.enumerated()), id: \.offset) { _, f in
                            if let t = Fmt.parseIST(f["ts"]?.string ?? ""),
                               let c = idx.last(where: { ($0["ts"]?.string ?? "") <= (f["ts"]?.string ?? "") })?["c"]?.number {
                                PointMark(x: .value("Time", t), y: .value("Index", c))
                                    .symbol(f["side"]?.string == "BUY" ? .triangle : .diamond)
                                    .foregroundStyle(f["side"]?.string == "BUY" ? Color.tdGood : Color.tdBad)
                            }
                        }
                    }
                    .chartYScale(domain: .automatic(includesZero: false))
                    .environment(\.timeZone, Fmt.ist)
                    .frame(height: 220)
                    HStack {
                        Button { playing.toggle(); if playing { Task { await play() } } } label: {
                            Image(systemName: playing ? "pause.fill" : "play.fill")
                        }
                        .buttonStyle(.borderless).accessibilityLabel(playing ? "Pause" : "Play")
                        Slider(value: $minute, in: 0...Double(max(1, idx.count - 1)), step: 1)
                        Text(nowTs.count >= 16 ? String(nowTs.dropFirst(11).prefix(5)) : "—").font(.caption.monospacedDigit())
                    }
                    let step = (d["steps"]?.array ?? []).last { ($0["ts"]?.string ?? "") <= nowTs + "~" }
                    LabeledContent("Realised so far", value: Fmt.rupees(step?["realised"]?.number ?? 0, hidden: hide, signed: true))
                        .monospacedDigit()
                } header: { Text("\(d["strategy"]?.string ?? "") · \(Fmt.marketDay(d["day"]?.string)) (IST)") }
                Section("Orders (\(fills.count))") {
                    ForEach(Array(fills.enumerated()), id: \.offset) { _, f in
                        HStack {
                            Text(f["side"]?.string ?? "").font(.caption.weight(.bold)).foregroundStyle(f["side"]?.string == "BUY" ? Color.tdGood : Color.tdBad)
                            Text(Fmt.contract(f["symbol"]?.string ?? "")).font(.caption)
                            Spacer()
                            Text("₹\(String(format: "%.2f", f["price"]?.number ?? 0)) · \(humanize(f["reason"]?.string ?? ""))").font(.caption.monospacedDigit())
                        }
                    }
                }
                LabeledContent("Day net after charges", value: Fmt.rupees(d["net"]?.number ?? 0, hidden: hide, signed: true)).monospacedDigit()
            }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Replay a day")
    }

    private func play() async {
        let count = Double((data?["index"]?.array ?? []).count - 1)
        if minute >= count { minute = 0 }
        while playing && minute < count {
            try? await Task.sleep(for: .milliseconds(80))
            minute += 1
        }
        playing = false
    }

    private func load() async {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = Fmt.ist
        do {
            var body: [String: JSONValue] = ["day": .string(f.string(from: day))]
            if let template { body["template"] = .string(template) }
            if let strategyID { body["strategy_id"] = .number(Double(strategyID)) }
            data = try await model.api.raw("POST", "/v1/research/replay", body: JSONValue.object(body))
            minute = 0
            message = nil
        } catch { message = error.localizedDescription; data = nil }
    }
}
