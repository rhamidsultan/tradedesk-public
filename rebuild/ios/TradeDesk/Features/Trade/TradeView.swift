import SwiftUI

/// The Trade tab: paper sessions running on the server (always marked PAPER, in blue, never mixed with real
/// money), today's PAPER P&L across them, and starting a new one from a strategy at the paper stage.
struct TradeView: View {
    @Environment(AppModel.self) private var model
    @State private var overview: JSONValue?
    @State private var sessions: [JSONValue] = []
    @State private var error: String?
    @State private var loaded = false
    @State private var showStart = false
    @State private var path = NavigationPath()
    @State private var confirmAll: String?        // "pause-all" | "square-off-all"
    @State private var note: String?

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let error {
                    Section {
                        Label(error, systemImage: "wifi.exclamationmark").foregroundStyle(Color.tdWarn)
                        Button("Retry") { Task { await load() } }
                    }
                }
                if let note { Section { Label(note, systemImage: "info.circle").font(.footnote) } }
                if let o = overview { overviewSection(o) }
                let active = sessions.filter { $0["status"]?.string != "stopped" }
                let ended = sessions.filter { $0["status"]?.string == "stopped" }
                Section {
                    if loaded && active.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("No paper sessions running.").font(.headline)
                            Text("Move a strategy to paper on its page (Stage), then start a session here. Paper uses live prices and never sends an order.")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        Button { showStart = true } label: {
                            Label("Start a paper session", systemImage: "play.circle").frame(maxWidth: .infinity, minHeight: 44)
                        }
                        .buttonStyle(.borderedProminent).tint(Color.tdPaper)
                    }
                    ForEach(Array(active.enumerated()), id: \.offset) { _, s in
                        NavigationLink(value: Int(s["id"]?.number ?? 0)) { SessionRow(s: s) }
                    }
                } header: { Text("Paper sessions") }
                Section("Market tools") {
                    NavigationLink { OptionChainToolView() } label: { Label("Option chain", systemImage: "tablecells") }
                    NavigationLink { AnalyserView() } label: { Label("Analyser", systemImage: "chart.xyaxis.line") }
                    NavigationLink { ScannerView() } label: { Label("Scanner", systemImage: "binoculars") }
                }
                if !ended.isEmpty {
                    Section("Ended") {
                        ForEach(Array(ended.prefix(20).enumerated()), id: \.offset) { _, s in
                            NavigationLink(value: Int(s["id"]?.number ?? 0)) { SessionRow(s: s) }
                        }
                    }
                }
                Section {
                    Label("Live trading is in Orders, after a strategy passes paper and 5 days of shadow.", systemImage: "bolt")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Trade")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Start paper", systemImage: "plus") { showStart = true }
                }
            }
            .navigationDestination(for: Int.self) { PaperSessionView(sessionID: $0) }
            .confirmationDialog(confirmAll == "pause-all" ? "Pause every running paper session?" : "Square off every paper session?",
                                isPresented: Binding(get: { confirmAll != nil }, set: { if !$0 { confirmAll = nil } }),
                                titleVisibility: .visible) {
                Button(confirmAll == "pause-all" ? "Pause all" : "Square off all", role: .destructive) {
                    if let c = confirmAll { Task { await all(c) } }
                }
            } message: { Text(allMessage) }
            .sheet(isPresented: $showStart) {
                StartPaperSheet { id in
                    showStart = false
                    Task { await load(); if let id { path.append(id) } }
                }
            }
            .refreshable { await load() }
            .task {
                // every 30 s while this tab is on screen; the server replays each minute
                while !Task.isCancelled {
                    await load()
                    try? await Task.sleep(for: .seconds(30))
                }
            }
        }
    }

    private func overviewSection(_ o: JSONValue) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    StatusPill(text: "PAPER", systemImage: "doc.text", color: .tdPaper)
                    if PaperClock.isStale(asOf: o["as_of"]?.string, live: o["day_pnl"]?.number != nil) {
                        StatusPill(text: "Stale · as of \(o["as_of"]?.string ?? "—")", systemImage: "clock.badge.exclamationmark", color: .tdWarn)
                    }
                    Spacer()
                    Text("\(Int(o["running"]?.number ?? 0)) running · \(Int(o["paused"]?.number ?? 0)) paused")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let day = o["day_pnl"]?.number {
                    PnlLine(label: "Today\(o["as_of"]?.string.map { " · as of \($0) IST" } ?? "")", value: day, big: true)
                } else if let last = o["last_day_pnl"]?.number {
                    PnlLine(label: "Last day · \(Fmt.marketDay(o["day_date"]?.string))", value: last, big: true)
                }
                PnlLine(label: "Total since each started", value: o["cum_net"]?.number ?? 0)
                if (o["open_legs"]?.number ?? 0) > 0 {
                    Text("\(Int(o["open_legs"]?.number ?? 0)) open legs").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(Array((o["kill"]?.array ?? []).enumerated()), id: \.offset) { _, k in
                    Label("Kill rule: \(k["name"]?.string ?? "")", systemImage: "exclamationmark.octagon.fill")
                        .font(.subheadline).foregroundStyle(Color.tdBad)
                }
                if !actionable.isEmpty {
                    HStack(spacing: 12) {
                        Button { confirmAll = "pause-all" } label: {
                            Label("Pause all", systemImage: "pause.fill").frame(maxWidth: .infinity, minHeight: 44)
                        }
                        .disabled(!actionable.contains { $0["status"]?.string == "running" })
                        Button(role: .destructive) { confirmAll = "square-off-all" } label: {
                            Label("Square off all", systemImage: "xmark.circle").frame(maxWidth: .infinity, minHeight: 44)
                        }
                        .tint(Color.tdBad)
                    }
                    .buttonStyle(.bordered)
                }
            }
            .padding(.vertical, 4)
        }
    }

    private var actionable: [JSONValue] {
        sessions.filter { ["running", "paused"].contains($0["status"]?.string ?? "") }
    }

    private var allMessage: String {
        let list = (confirmAll == "pause-all" ? actionable.filter { $0["status"]?.string == "running" } : actionable)
        let lines = list.map { s in
            "\(s["name"]?.string ?? "") · \(Int(s["open_legs"]?.number ?? 0)) open · \(Fmt.rupees(s["day_pnl"]?.number ?? 0, signed: true))"
        }
        let what = confirmAll == "pause-all" ? "No new entries; open legs keep their stops." : "Every open leg exits at the next minute (or the next open) and each session stays paused."
        return "PAPER. " + what + "\n" + lines.joined(separator: "\n")
    }

    private func all(_ what: String) async {
        let list = (what == "pause-all" ? actionable.filter { $0["status"]?.string == "running" } : actionable)
        let ids = list.compactMap { $0["id"]?.number }.map(JSONValue.number)
        do {
            let r = try await model.api.raw("POST", "/v1/paper/\(what)", body: JSONValue.object(["ids": .array(ids)]))
            note = r["note"]?.string
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func load() async {
        do {
            async let o = model.api.raw("GET", "/v1/paper/overview")
            async let s = model.api.raw("GET", "/v1/paper/sessions")
            overview = try await o
            sessions = try await s.array ?? []
            error = nil
            PaperActivity.shared.update(sessions: sessions)
        } catch {
            self.error = "Couldn't load paper sessions: \(error.localizedDescription)"
        }
        loaded = true
    }
}

/// A rupee figure with a sign and a word, never colour alone.
struct PnlLine: View {
    @AppStorage("hideAmounts") private var hide = false
    let label: String
    let value: Double
    var big = false
    var mode = "Paper"           // spoken first by VoiceOver: Paper, Shadow or Live

    var body: some View {
        HStack {
            Text(label).font(big ? .subheadline : .caption).foregroundStyle(.secondary)
            Spacer()
            Text(Fmt.rupees(value, hidden: hide, signed: true))
                .font(big ? .title2.weight(.semibold) : .subheadline)
                .monospacedDigit()
                .foregroundStyle(value > 0 ? Color.tdGood : value < 0 ? Color.tdBad : .primary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(mode), \(label): " + (hide ? "amount hidden" : value == 0 ? "no change" : "\(value > 0 ? "profit" : "loss") \(Fmt.rupees(abs(value)))"))
    }
}

struct SessionRow: View {
    let s: JSONValue

    private var stale: Bool { PaperClock.isStale(asOf: s["as_of"]?.string, live: s["live"]?.bool == true) }

    /// Started after hours (or before its first bar): it has not run yet.
    private var notStarted: Bool {
        guard s["status"]?.string == "running", s["day_pnl"]?.number == nil, let f = s["first_day"]?.string else { return false }
        return s["as_of"]?.string == nil && f >= todayIST
    }

    private var todayIST: String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = Fmt.ist
        return f.string(from: .now)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(s["name"]?.string ?? "").font(.headline).lineLimit(2)
                Spacer()
                if stale { StatusPill(text: "Stale", systemImage: "clock.badge.exclamationmark", color: .tdWarn) }
                statusPill
            }
            HStack {
                if let d = s["day_pnl"]?.number {
                    Text((s["live"]?.bool == true ? "Today " : "Last day ") + Fmt.rupees(d, signed: true))
                        .foregroundStyle(stale ? .secondary : d > 0 ? Color.tdGood : d < 0 ? Color.tdBad : .primary)
                }
                Spacer()
                if let c = s["cum_net"]?.number { Text("Total " + Fmt.rupees(c, signed: true)).foregroundStyle(.secondary) }
            }
            .font(.subheadline).monospacedDigit()
            HStack(spacing: 8) {
                Text(s["underlying"]?.string ?? "")
                if let n = s["open_legs"]?.number, n > 0 { Text("· \(Int(n)) open") }
                if let a = s["as_of"]?.string { Text("· as of \(a)") }
                if s["error"]?.string != nil { Label("problem", systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            }
            .font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                if let pct = s["nearest_stop_pct"]?.number {
                    Label(pct <= 0 ? "Stop crossed" : "Stop \(Int(pct.rounded()))% away",
                          systemImage: pct < 20 ? "exclamationmark.triangle.fill" : "shield")
                        .foregroundStyle(pct < 20 ? Color.tdWarn : .secondary)
                }
                if let m = s["max_loss_per_day"]?.number { Text("max \(Fmt.rupees(m)) a day").foregroundStyle(.secondary) }
            }
            .font(.caption)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var statusPill: some View {
        switch s["status"]?.string {
        case "running" where notStarted:
            StatusPill(text: "Starts \(Fmt.marketDay(s["first_day"]?.string))", systemImage: "calendar", color: .tdPaper)
        case "running": StatusPill(text: "Running", systemImage: "play.fill", color: .tdPaper)
        case "paused": StatusPill(text: (s["kill"]?.array?.isEmpty == false) ? "Kill rule" : "Paused",
                                  systemImage: "pause.fill", color: .tdWarn)
        case "stopping": StatusPill(text: "Stopping", systemImage: "stop.fill", color: .tdWarn)
        default: StatusPill(text: "Ended", systemImage: "checkmark", color: .secondary)
        }
    }
}

/// Pick a strategy at the paper stage (and a preset), then the deploy sheet.
struct StartPaperSheet: View {
    @Environment(AppModel.self) private var model
    var done: (Int?) -> Void
    @State private var strategies: [JSONValue] = []
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        NavigationStack {
            List {
                if let error {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn)
                    Button("Retry") { Task { await load() } }
                }
                let ready = strategies.filter { ["paper", "shadow", "live"].contains($0["stage"]?.string ?? "") }
                if loaded && ready.isEmpty {
                    Text("No strategy is at the paper stage yet. On a strategy's page, Stage › Move to paper (after an honest-fills backtest passes its checks).")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                ForEach(Array(ready.enumerated()), id: \.offset) { _, s in
                    NavigationLink {
                        DeploySheet(strategyID: Int(s["id"]?.number ?? 0), done: done)
                    } label: {
                        VStack(alignment: .leading) {
                            Text(s["name"]?.string ?? "").font(.headline)
                            Text("Version \(Int(s["version"]?.number ?? 0)) · \(Int(s["presets"]?.number ?? 0)) presets")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("Start paper")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { done(nil) } } }
            .task { await load() }
        }
    }

    private func load() async {
        do { strategies = try await model.api.raw("GET", "/v1/strategies/studio/list").array ?? []; error = nil }
        catch { self.error = error.localizedDescription }
        loaded = true
    }
}
