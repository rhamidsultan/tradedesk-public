import SwiftUI

/// Trade › Scanner: built-in setups checked on the latest stored bars of every index and stock.
struct ScannerView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("scanSegment") private var segment = "all"
    @AppStorage("scanSetups") private var chosenRaw = ""        // comma list; empty = all
    @State private var data: JSONValue?
    @State private var error: String?
    @State private var loading = false

    private var chosen: Set<String> { Set(chosenRaw.split(separator: ",").map(String.init)) }

    var body: some View {
        List {
            Section {
                Picker("Look at", selection: $segment) {
                    Text("All").tag("all"); Text("Indexes").tag("idx"); Text("Stocks").tag("eq")
                }
                .pickerStyle(.segmented)
                Menu {
                    Button("All setups") { chosenRaw = "" }
                    ForEach(setups) { s in
                        Button {
                            var c = chosen.isEmpty ? Set(setups.map(\.id)) : chosen
                            if c.contains(s.id) { c.remove(s.id) } else { c.insert(s.id) }
                            chosenRaw = c.count == setups.count ? "" : c.sorted().joined(separator: ",")
                        } label: {
                            Label(s.name, systemImage: (chosen.isEmpty || chosen.contains(s.id)) ? "checkmark.circle.fill" : "circle")
                        }
                    }
                } label: {
                    LabeledContent("Setups", value: chosen.isEmpty ? "All \(setups.count)" : "\(chosen.count) of \(setups.count)")
                }
            }
            if let error {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn)
                    Button("Retry") { Task { await load() } }
                }
            }
            if let d = data {
                let results = d["results"]?.array ?? []
                Section {
                    if results.isEmpty {
                        Text("No setup fired on the \(Int(d["scanned"]?.number ?? 0)) symbols scanned.").foregroundStyle(.secondary)
                    }
                    ForEach(Array(results.enumerated()), id: \.offset) { _, r in hitRow(r) }
                } header: {
                    Text("\(results.count) of \(Int(d["scanned"]?.number ?? 0)) symbols with a setup")
                } footer: {
                    Text("Closed bars only, from the stored 1-minute data (indexes and stocks recorded on this server). A setup is a starting point to look closer, not an order.")
                }
            } else if loading {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("Scanner")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task(id: "\(segment)|\(chosenRaw)") { await load() }
    }

    private struct Setup: Identifiable { let id: String; let name: String }

    private var setups: [Setup] {
        (data?["setups"]?.array ?? []).compactMap { s in
            guard let id = s["id"]?.string, let n = s["name"]?.string else { return nil }
            return Setup(id: id, name: n)
        }
    }

    private func hitRow(_ r: JSONValue) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(r["symbol"]?.string ?? "").font(.headline)
                if r["segment"]?.string == "idx" { Text("Index").font(.caption2).foregroundStyle(.secondary) }
                Spacer()
                Text(num(r["last"]?.number)).monospacedDigit()
                if let c = r["change_pct"]?.number {
                    Text("\(c >= 0 ? "▲" : "▼") \(num(abs(c)))%").font(.caption).monospacedDigit()
                        .foregroundStyle(c > 0 ? Color.tdGood : c < 0 ? Color.tdBad : .secondary)
                }
            }
            ForEach(Array((r["hits"]?.array ?? []).enumerated()), id: \.offset) { _, h in
                let dir = h["direction"]?.string ?? ""
                Label {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(h["name"]?.string ?? "").font(.subheadline)
                        Text(h["detail"]?.string ?? "").font(.caption).foregroundStyle(.secondary)
                    }
                } icon: {
                    Image(systemName: dir == "long" ? "arrow.up.right" : dir == "short" ? "arrow.down.right" : "eye")
                        .foregroundStyle(dir == "long" ? Color.tdGood : dir == "short" ? Color.tdBad : Color.tdWarn)
                }
                .accessibilityLabel("\(dir == "long" ? "Bullish" : dir == "short" ? "Bearish" : "Watch"): \(h["name"]?.string ?? ""). \(h["detail"]?.string ?? "")")
            }
            Text("\(Fmt.marketDay(r["day"]?.string)) · as of \(r["as_of"]?.string ?? "") IST").font(.caption2).foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    private func load() async {
        loading = true
        defer { loading = false }
        var q = "/v1/tools/scan?segment=\(segment)"
        if !chosenRaw.isEmpty { q += "&setups=\(chosenRaw)" }
        do { data = try await model.api.raw("GET", q); error = nil }
        catch { self.error = "Couldn't scan: \(error.localizedDescription)" }
    }
}
