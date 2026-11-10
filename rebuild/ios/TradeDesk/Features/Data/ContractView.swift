import Charts
import SwiftUI

/// One option contract through one stored day: price chart and the day's high, low, volume and OI.
struct ContractView: View {
    @Environment(AppModel.self) private var model
    let underlying: String
    let symbol: String
    let day: String
    @State private var bars: [Bar] = []
    @State private var loaded = false
    @State private var error: String?

    var body: some View {
        List {
            if let error {
                Label("Couldn't load: \(error)", systemImage: "wifi.slash").foregroundStyle(Color.tdWarn)
                Button("Retry") { Task { await load() } }
            } else if bars.isEmpty {
                Text(loaded ? "No trades stored for this contract on this day." : "Loading…").foregroundStyle(.secondary)
            } else {
                Section("Price (IST)") {
                    Chart(bars) { b in
                        if let t = b.date {
                            RuleMark(x: .value("Time", t), yStart: .value("Low", b.l), yEnd: .value("High", b.h))
                                .foregroundStyle(.secondary.opacity(0.4))
                            LineMark(x: .value("Time", t), y: .value("Close", b.c))
                        }
                    }
                    .chartYScale(domain: .automatic(includesZero: false))
                    .environment(\.timeZone, Fmt.ist)
                    .frame(height: 220)
                }
                Section("Day") {
                    LabeledContent("Open", value: String(format: "%.2f", bars.first!.o))
                    LabeledContent("High", value: String(format: "%.2f", bars.map(\.h).max() ?? 0))
                    LabeledContent("Low", value: String(format: "%.2f", bars.map(\.l).min() ?? 0))
                    LabeledContent("Close", value: String(format: "%.2f", bars.last!.c))
                    LabeledContent("Volume", value: "\(bars.compactMap(\.v).reduce(0, +))")
                    if let oi = bars.last?.oi { LabeledContent("Open interest", value: "\(oi)") }
                    LabeledContent("Minutes with trades", value: "\(bars.count)")
                }
                .monospacedDigit()
            }
        }
        .navigationTitle(Fmt.contract(symbol))
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func load() async {
        do { bars = try await model.api.get("/v1/data/\(underlying)/contract?symbol=\(symbol)&day=\(day)"); error = nil }
        catch { self.error = error.localizedDescription }
        loaded = true
    }
}
