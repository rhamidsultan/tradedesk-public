import SwiftUI

/// More › Data: what is stored for each index, how current it is, and disk use.
struct DataView: View {
    @Environment(AppModel.self) private var model
    @State private var summary: DataSummary?
    @State private var error: String?

    var body: some View {
        List {
            if let error {
                Label(error, systemImage: "wifi.slash").foregroundStyle(.secondary)
                Button("Retry") { Task { await load() } }
            } else if let s = summary, !s.available {
                ContentUnavailableView("No market data on the server yet", systemImage: "externaldrive.badge.xmark",
                                       description: Text("Copy the store to the server, or wait for the first recording after the close."))
            } else if let s = summary {
                Section {
                    NavigationLink(value: Route.recorder) { Label("Recording today's prices", systemImage: "record.circle") }
                }
                Section("Indexes") {
                    ForEach(s.indexes) { i in
                        NavigationLink(value: Route.dataIndex(i.underlying)) { IndexRow(index: i) }
                    }
                    if let v = s.vixTo { LabeledContent("India VIX to", value: Fmt.marketDay(v)) }
                }
                Section("Disk use") {
                    let total = s.disk.reduce(0) { $0 + $1.bytes }
                    LabeledContent("Total", value: Fmt.bytes(total)).font(.headline)
                    ForEach(s.disk.prefix(8)) { d in
                        LabeledContent(d.folder, value: Fmt.bytes(d.bytes)).font(.subheadline.monospacedDigit())
                    }
                }
            } else {
                ProgressView()
            }
        }
        .navigationTitle("Market data")
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        do { summary = try await model.api.get("/v1/data/summary"); error = nil }
        catch { self.error = error.localizedDescription }
    }
}

private struct IndexRow: View {
    let index: DataSummary.Index

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(index.underlying).font(.headline)
                Spacer()
                freshness
            }
            Text("Index \(Fmt.marketDay(index.spotFrom)) – \(Fmt.marketDay(index.spotTo)) · \(index.spotDays) days")
                .font(.caption).foregroundStyle(.secondary)
            if index.optionsDays > 0 {
                Text("Options \(Fmt.marketDay(index.optionsFrom)) – \(Fmt.marketDay(index.optionsTo)) · \(index.optionsDays) days")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("No option prices stored").font(.caption).foregroundStyle(Color.tdWarn)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var freshness: some View {
        if index.behindDays == nil {
            StatusPill(text: "Unknown", systemImage: "questionmark", color: .secondary)
        } else if (index.behindDays ?? 0) == 0 {
            StatusPill(text: "Up to date", systemImage: "checkmark", color: .tdGood)
        } else {
            let behind = index.behindDays ?? 0
            StatusPill(text: "\(behind) day\(behind == 1 ? "" : "s") behind", systemImage: "clock.badge.exclamationmark",
                       color: behind > 2 ? .tdBad : .tdWarn)
        }
    }
}
