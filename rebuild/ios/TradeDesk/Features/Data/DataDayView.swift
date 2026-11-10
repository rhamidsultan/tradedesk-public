import Charts
import SwiftUI

/// One day: what was checked, the index through the day, and the option chains for that day.
struct DataDayView: View {
    @Environment(AppModel.self) private var model
    let underlying: String
    let day: String
    @State private var bars: [Bar] = []
    @State private var info: DataMonth.Day?
    @State private var expiries: [ExpiryInfo] = []
    @State private var loading = true
    @State private var error: String?

    var body: some View {
        List {
            if let error {
                Label("Couldn't load: \(error)", systemImage: "wifi.slash").foregroundStyle(Color.tdWarn)
                Button("Retry") { Task { await load() } }
            }
            if let info {
                Section("Checks") {
                    if info.issues.isEmpty { Label("Complete", systemImage: "checkmark.circle.fill").foregroundStyle(Color.tdGood) }
                    ForEach(info.issues, id: \.self) { i in
                        Label(i.text, systemImage: levelIcon(i.level)).foregroundStyle(levelColor(i.level))
                    }
                    LabeledContent("Index minutes", value: "\(info.spotBars)")
                    LabeledContent("Option contracts", value: "\(info.optionContracts)")
                    LabeledContent("India VIX", value: info.vix ? "Yes" : "No")
                }
            }
            Section("\(underlying) through the day (IST)") {
                if bars.isEmpty {
                    Text(loading ? "Loading…" : "No index bars stored for this day.").foregroundStyle(.secondary)
                } else {
                    Chart(bars) { b in
                        if let t = b.date { LineMark(x: .value("Time", t), y: .value("Close", b.c)) }
                    }
                    .chartYScale(domain: .automatic(includesZero: false))
                    .chartXAxis { AxisMarks(values: .stride(by: .hour)) { _ in AxisGridLine(); AxisValueLabel(format: .dateTime.hour()) } }
                    .environment(\.timeZone, Fmt.ist)
                    .frame(height: 200)
                    .accessibilityLabel("Index from \(bars.first.map { String(format: "%.0f", $0.c) } ?? "") to \(bars.last.map { String(format: "%.0f", $0.c) } ?? "")")
                }
            }
            let live = expiries.filter { $0.from <= day && $0.to >= day && $0.expiry >= day }
            if !live.isEmpty {
                Section("Option chains on this day") {
                    ForEach(live.prefix(6)) { e in
                        NavigationLink(value: Route.chain(underlying, e.expiry, day)) {
                            Text("Expiry \(Fmt.marketDay(e.expiry))")
                        }
                    }
                }
            }
        }
        .navigationTitle(Fmt.marketDay(day))
        .task { await load() }
    }

    private func load() async {
        loading = true; defer { loading = false }
        let api = model.api
        async let b: [Bar] = api.get("/v1/data/\(underlying)/index?day=\(day)")
        async let m: DataMonth = api.get("/v1/data/\(underlying)/month?m=\(day.prefix(7))")
        async let e: [ExpiryInfo] = api.get("/v1/data/\(underlying)/expiries")
        do {
            bars = try await b
            info = try await m.days.first { $0.date == day }
            expiries = try await e
            error = nil
        } catch { self.error = error.localizedDescription }
    }
}
