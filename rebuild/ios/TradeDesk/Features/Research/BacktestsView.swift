import SwiftUI

/// Backtest results: every run, or one strategy's (opened from that strategy's "Backtests" link).
/// Star, archive or delete by swiping; open a run for its result.
struct BacktestsView: View {
    @Environment(AppModel.self) private var model
    @State var strategyID: Int?
    let strategyName: String?
    @State private var runs: [BacktestRunItem] = []
    @State private var starred = false
    @State private var loaded = false
    @State private var error: String?
    @State private var confirmDelete: BacktestRunItem?
    @State private var poll: Task<Void, Never>?

    init(strategyID: Int?, strategyName: String?) {
        _strategyID = State(initialValue: strategyID)
        self.strategyName = strategyName
    }

    var body: some View {
        List {
            if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            Section {
                if strategyID != nil {
                    HStack {
                        Label(strategyName ?? "One strategy", systemImage: "line.3.horizontal.decrease.circle.fill")
                            .foregroundStyle(.tint)
                        Spacer()
                        Button("Show all") { strategyID = nil }.buttonStyle(.borderless)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityHint("Showing only this strategy's backtests")
                }
                Picker("Runs", selection: $starred) { Text("All runs").tag(false); Text("Starred").tag(true) }
                    .pickerStyle(.segmented)
            }
            Section {
                if !loaded {
                    ProgressView()
                } else if runs.isEmpty {
                    Text(starred ? "No starred runs. Swipe a run to star it." : "No backtests yet.")
                        .foregroundStyle(.secondary)
                }
                ForEach(runs) { r in
                    NavigationLink(value: r) { RunRow(run: r) }
                        .swipeActions {
                            if r.status != "running" {
                                Button("Delete", systemImage: "trash", role: .destructive) { confirmDelete = r }
                            }
                            Button("Archive", systemImage: "archivebox") { Task { await archive(r) } }
                            Button(r.starred ? "Unstar" : "Star", systemImage: r.starred ? "star.slash" : "star") {
                                Task { await star(r) }
                            }.tint(.yellow)
                        }
                }
            } footer: {
                if !runs.isEmpty { Text("Swipe left on a run to star, archive or delete it.") }
            }
        }
        .navigationTitle("Backtest results")
        .confirmationDialog("Delete this backtest?", isPresented: Binding(get: { confirmDelete != nil },
                                                                           set: { if !$0 { confirmDelete = nil } }),
                            titleVisibility: .visible, presenting: confirmDelete) { r in
            Button("Delete", role: .destructive) { Task { await delete(r) } }
        } message: { r in Text("\(r.name). Its results go too; this cannot be undone.") }
        .task(id: "\(strategyID ?? 0)-\(starred)") { await load() }
        .refreshable { await load() }
        .onAppear { startPolling() }
        .onDisappear { poll?.cancel() }
    }

    private var query: String {
        var q: [String] = []
        if starred { q.append("starred=true") }
        if let strategyID { q.append("strategy_id=\(strategyID)") }
        return q.isEmpty ? "" : "?" + q.joined(separator: "&")
    }

    private func load() async {
        do { runs = try await model.api.get("/v1/research/backtests" + query); error = nil }
        catch { self.error = error.localizedDescription }
        loaded = true
    }

    private func startPolling() {
        poll?.cancel()
        poll = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                if runs.contains(where: { $0.status == "queued" || $0.status == "running" }) { await load() }
            }
        }
    }

    private func star(_ r: BacktestRunItem) async {
        struct B: Encodable { let starred: Bool }
        let _: BacktestRunItem? = try? await model.api.patch("/v1/research/backtests/\(r.id)", B(starred: !r.starred))
        await load()
    }

    private func archive(_ r: BacktestRunItem) async {
        struct B: Encodable { let archived: Bool }
        let _: BacktestRunItem? = try? await model.api.patch("/v1/research/backtests/\(r.id)", B(archived: true))
        await load()
    }

    private func delete(_ r: BacktestRunItem) async {
        struct R: Decodable { let ok: Bool }
        do {
            let _: R = try await model.api.send("DELETE", "/v1/research/backtests/\(r.id)", body: Optional<String>.none)
            error = nil
        } catch { self.error = error.localizedDescription }
        await load()
    }
}
