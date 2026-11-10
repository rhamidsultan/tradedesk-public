import SwiftUI

/// Strategies tab (phase 2): run a template as a backtest, and the saved runs with their results.
/// The no-code builder, Python strategies and Studio arrive in phase 3.
struct StrategiesView: View {
    @Environment(AppModel.self) private var model
    @State private var templates: [StrategyTemplate] = []
    @State private var mine: [JSONValue] = []
    @State private var gallery: [JSONValue] = []
    @State private var showImport = false
    @State private var showCompare = false
    @State private var path = NavigationPath()
    @State private var runs: [BacktestRunItem] = []
    @State private var error: String?
    @State private var showStarred = false
    @State private var poll: Task<Void, Never>?
    @State private var loaded = false

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let error { Label(error, systemImage: "wifi.slash").foregroundStyle(Color.tdWarn) }
                Section {
                    if mine.isEmpty && loaded {
                        Text("None yet. Customise a template below or start blank.").foregroundStyle(.secondary)
                    }
                    ForEach(Array(mine.enumerated()), id: \.offset) { _, d in
                        Button { path.append(StrategyRoute.strategy(Int(d["id"]?.number ?? 0))) } label: { MyStrategyRow(doc: d) }
                            .buttonStyle(.plain)
                    }
                } header: { Text("My strategies") }
                Section {
                    Picker("Runs", selection: $showStarred) { Text("All runs").tag(false); Text("Starred").tag(true) }
                        .pickerStyle(.segmented)
                    if !loaded {
                        ProgressView()
                    } else if runs.isEmpty {
                        Text(showStarred ? "No starred runs. Swipe a run to star it." : "No backtests yet. Pick a template below.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(runs) { r in
                        NavigationLink(value: r) { RunRow(run: r) }
                            .swipeActions {
                                Button(r.starred ? "Unstar" : "Star", systemImage: r.starred ? "star.slash" : "star") {
                                    Task { await star(r) }
                                }.tint(.yellow)
                                Button("Archive", systemImage: "archivebox") { Task { await archive(r) } }
                            }
                    }
                } header: { Text("Backtests") }
                let groups = Dictionary(grouping: gallery) { $0["category"]?.string ?? "Other" }
                ForEach(groups.keys.sorted(), id: \.self) { cat in
                    Section(cat) {
                        ForEach(Array((groups[cat] ?? []).enumerated()), id: \.offset) { _, g in
                            let t = templates.first { $0.id == g["id"]?.string }
                            VStack(alignment: .leading, spacing: 4) {
                                Text(g["name"]?.string ?? "").font(.headline)
                                HStack(spacing: 6) {
                                    ForEach((g["chips"]?.array ?? []).compactMap(\.string), id: \.self) { c in
                                        Text(c).font(.caption2.weight(.semibold)).padding(.horizontal, 7).padding(.vertical, 3)
                                            .background(.quaternary, in: .capsule)
                                    }
                                }
                                if let t { Text("\(t.underlying) · stop \(stopText(t)) · max loss a day \(Fmt.rupees(t.maxLossPerDay))").font(.caption).foregroundStyle(.secondary) }
                                HStack {
                                    Button("Customise") { Task { await customise(g["id"]?.string) } }.buttonStyle(.glass)
                                    if let t { Button("Backtest") { path.append(StrategyRoute.template(t)) }.buttonStyle(.glass) }
                                    if let t {
                                        Menu {
                                            Button("Find best settings", systemImage: "slider.horizontal.below.square.and.square.filled") { path.append(StrategyRoute.sweep(t.id)) }
                                            Button("Replay a day", systemImage: "play.circle") { path.append(StrategyRoute.replay(t.id)) }
                                        } label: { Image(systemName: "ellipsis.circle") }
                                        .accessibilityLabel("More for \(t.name)")
                                    }
                                }
                                .font(.subheadline)
                            }
                            .padding(.vertical, 2)
                        }
                    }
                }
            }
            .navigationTitle("Strategies")
            .toolbar {
                Menu {
                    Button("Start blank", systemImage: "doc.badge.plus") { Task { await customise(nil) } }
                    Button("Import a share code", systemImage: "square.and.arrow.down") { showImport = true }
                    Button("Compare runs", systemImage: "square.split.2x1") { showCompare = true }
                } label: { Image(systemName: "plus") }
                .accessibilityLabel("New strategy")
            }
            .sheet(isPresented: $showCompare) {
                NavigationStack { ComparePicker(runs: runs.filter { $0.status == "done" && $0.kind != "sweep" }) }
            }
            .sheet(isPresented: $showImport) {
                NavigationStack { ImportView { id in showImport = false; Task { await load(); if let id { path.append(StrategyRoute.editor(id)) } } } }
            }
            .navigationDestination(for: StrategyRoute.self) { r in
                switch r {
                case .template(let t): BacktestFormView(template: t) { await load() }
                case .strategy(let id): StrategyDetailView(strategyID: id)
                case .editor(let id): StrategyEditor(strategyID: id)
                case .sweep(let template): SweepFormView(template: template, specFromRun: nil)
                case .replay(let template): ReplayView(template: template)
                }
            }
            .navigationDestination(for: BacktestRunItem.self) { r in
                if r.kind == "sweep" { SweepResultView(runID: r.id) } else { ResultView(runID: r.id) }
            }
            .navigationDestination(for: TradeItem.self) { t in TradeDetailView(trade: t) }
            .task(id: showStarred) { await load() }
            .onChange(of: model.tab) { _, t in if t == .strategies { Task { await loadRuns() } } }
            .refreshable { await load() }
            .onAppear { startPolling() }
            .onDisappear { poll?.cancel() }
        }
    }

    private func stopText(_ t: StrategyTemplate) -> String {
        guard let s = t.stopLoss, let v = s.value else { return "set" }
        switch s.type { case "multiple": return "\(v.formatted())× premium"; case "pct": return "\(v.formatted())%"
        default: return "\(v.formatted()) \(s.type ?? "")" }
    }

    private func startPolling() {
        poll?.cancel()
        poll = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                if runs.contains(where: { $0.status == "queued" || $0.status == "running" }) { await loadRuns() }
            }
        }
    }

    private func load() async {
        let api = model.api
        async let t: [StrategyTemplate] = api.get("/v1/research/templates")
        async let g = api.raw("GET", "/v1/strategies/gallery")
        async let m = api.raw("GET", "/v1/strategies")
        await loadRuns()
        do {
            templates = try await t
            gallery = try await g.array ?? []
            mine = try await m.array ?? []
        } catch { self.error = error.localizedDescription }
    }

    /// Customise a template (or start blank) into the builder; the draft is kept on the server.
    private func customise(_ template: String?) async {
        struct B: Encodable { let template: String?; let blank: Bool }
        do {
            let d = try await model.api.raw("POST", "/v1/strategies", body: B(template: template, blank: template == nil))
            if let id = d["id"]?.number { await load(); path.append(StrategyRoute.editor(Int(id))) }
        } catch { self.error = error.localizedDescription }
    }

    private func loadRuns() async {
        do { runs = try await model.api.get("/v1/research/backtests\(showStarred ? "?starred=true" : "")"); error = nil }
        catch { self.error = error.localizedDescription }
        loaded = true
    }

    private func star(_ r: BacktestRunItem) async {
        struct B: Encodable { let starred: Bool }
        let _: BacktestRunItem? = try? await model.api.patch("/v1/research/backtests/\(r.id)", B(starred: !r.starred))
        await loadRuns()
    }

    private func archive(_ r: BacktestRunItem) async {
        struct B: Encodable { let archived: Bool }
        let _: BacktestRunItem? = try? await model.api.patch("/v1/research/backtests/\(r.id)", B(archived: true))
        await loadRuns()
    }
}

enum StrategyRoute: Hashable {
    case template(StrategyTemplate)
    case strategy(Int)
    case editor(Int)
    case sweep(String)
    case replay(String)
}

struct MyStrategyRow: View {
    @AppStorage("hideAmounts") private var hide = false
    let doc: JSONValue

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(doc["name"]?.string ?? "").font(.headline)
                Spacer()
                let v = Int(doc["version"]?.number ?? 0)
                StatusPill(text: v == 0 ? "Draft" : "v\(v)", systemImage: v == 0 ? "pencil" : "checkmark", color: v == 0 ? .tdWarn : .secondary)
            }
            HStack(spacing: 8) {
                if doc["has_draft"]?.bool == true, (doc["version"]?.number ?? 0) > 0 {
                    Label("Unsaved changes", systemImage: "pencil.circle").font(.caption).foregroundStyle(Color.tdWarn)
                }
                if let net = doc["last_backtest.summary.net_pnl"]?.number {
                    Text("Last backtest \(Fmt.rupees(net, hidden: hide, signed: true))").font(.caption.monospacedDigit())
                        .foregroundStyle(net >= 0 ? Color.tdGood : Color.tdBad)
                }
            }
        }
        .contentShape(.rect)
        .accessibilityElement(children: .combine)
    }
}

struct RunRow: View {
    @AppStorage("hideAmounts") private var hide = false
    let run: BacktestRunItem

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                if run.starred { Image(systemName: "star.fill").foregroundStyle(.yellow).accessibilityLabel("Starred") }
                Text(run.name).font(.headline).lineLimit(1)
                Spacer()
                status
            }
            Text("\(Fmt.marketDay(run.start)) – \(Fmt.marketDay(run.end)) · \(run.fillModel == "honest" ? "honest fills" : "classic fills")")
                .font(.caption).foregroundStyle(.secondary)
            if run.status == "done", let s = run.summary, let net = s.netPnl {
                HStack(spacing: 10) {
                    Text("Net \(Fmt.rupees(net, hidden: hide, signed: true))")
                        .foregroundStyle(net >= 0 ? Color.tdGood : Color.tdBad).font(.subheadline.weight(.semibold))
                    if let dd = s.maxDrawdown { Text("DD \(Fmt.rupees(-dd, hidden: hide))").font(.caption) }
                    if let n = s.trades { Text("\(n) trades").font(.caption) }
                    if s.luckWarning == true { Label("Few trades: luck?", systemImage: "dice").font(.caption).foregroundStyle(Color.tdWarn) }
                }
                .monospacedDigit()
            } else if !run.note.isEmpty {
                Text(run.note).font(.caption).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var status: some View {
        switch run.status {
        case "queued": StatusPill(text: "Waiting", systemImage: "hourglass", color: .secondary)
        case "running": StatusPill(text: "\(run.progress)%", systemImage: "gearshape.2", color: .tdPaper)
        case "failed": StatusPill(text: "Failed", systemImage: "xmark", color: .tdBad)
        case "cancelled": StatusPill(text: "Cancelled", systemImage: "stop", color: .secondary)
        default:
            if run.kind == "sweep" { StatusPill(text: "Sweep", systemImage: "square.grid.3x3", color: .tdPaper) }
            if (run.costStress ?? 1) >= 2 { StatusPill(text: "Conservative", systemImage: "shield", color: .secondary) }
        }
    }
}

/// Ask for a backtest of a template: dates and the fill model.
struct BacktestFormView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let template: StrategyTemplate
    var done: () async -> Void
    @State private var start = Calendar.current.date(byAdding: .year, value: -1, to: .now) ?? .now
    @State private var end = Date.now
    @State private var honest = true
    @State private var conservative = false
    @State private var name = ""
    @State private var message: String?
    @State private var sending = false

    var body: some View {
        Form {
            Section {
                Text(template.notes.isEmpty ? template.name : template.notes).font(.footnote)
            }
            Section("Run") {
                TextField("Name", text: $name, prompt: Text(template.name))
                DatePicker("From", selection: $start, displayedComponents: .date)
                DatePicker("To", selection: $end, in: start..., displayedComponents: .date)
                Toggle("Honest fills", isOn: $honest)
                Toggle("Conservative (spread and slippage × 2)", isOn: $conservative)
            }
            Section {
                Button {
                    Task { await send() }
                } label: {
                    HStack { Text("Run backtest"); if sending { Spacer(); ProgressView() } }
                }
                .disabled(sending)
            } footer: {
                Text(honest
                     ? "Honest fills: cheap options pay at least ₹0.10–0.20 a side, fills only at trades at most 3 minutes old, an order waits up to 15 minutes for a trade. Results are net of all charges."
                     : "Classic fills: the earlier TradeDesk model, kept for comparing with older results.")
                + Text(" During market hours runs wait for the close.")
            }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle(template.name)
        .navigationBarTitleDisplayMode(.inline)
    }

    private func send() async {
        sending = true; defer { sending = false }
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = Fmt.ist
        struct B: Encodable { let template: String; let start: String; let end: String; let name: String?; let fillModel: String; let fills: String }
        do {
            let _: BacktestRunItem = try await model.api.post("/v1/research/backtests", B(
                template: template.id, start: f.string(from: start), end: f.string(from: end),
                name: name.isEmpty ? nil : name, fillModel: honest ? "honest" : "classic",
                fills: conservative ? "conservative" : "normal"))
            await done()
            dismiss()
        } catch { message = error.localizedDescription }
    }
}
