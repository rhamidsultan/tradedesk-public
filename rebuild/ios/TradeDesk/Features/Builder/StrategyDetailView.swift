import SwiftUI

/// One of my strategies: versions, last backtest, and Edit, Backtest, Share, Export, Archive.
struct StrategyDetailView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    let strategyID: Int
    @State private var doc: JSONValue?
    @State private var shareCode: String?
    @State private var withResults = false
    @State private var exportText: String?
    @State private var message: String?
    @State private var showBacktest = false
    @State private var confirmArchive = false
    @State private var restoreVersion: Int?

    var body: some View {
        List {
            if let d = doc {
                Section {
                    LabeledContent("Version", value: Int(d["version"]?.number ?? 0) == 0 ? "Not saved yet" : "v\(Int(d["version"]?.number ?? 0))")
                    if d["editing_draft"]?.bool == true {
                        Label("Unsaved changes in the draft", systemImage: "pencil.circle").foregroundStyle(Color.tdWarn)
                    }
                    if let lb = d["last_backtest"], lb != .null {
                        let net = lb["summary.net_pnl"]?.number ?? 0
                        LabeledContent("Last backtest (v\(Int(lb["version"]?.number ?? 0)))") {
                            Text(Fmt.rupees(net, hidden: hide, signed: true)).foregroundStyle(net >= 0 ? Color.tdGood : Color.tdBad)
                        }
                        .monospacedDigit()
                    }
                }
                StageSection(strategyID: strategyID)
                PresetsSection(strategyID: strategyID, saved: Int(d["version"]?.number ?? 0) > 0)
                Section {
                    NavigationLink { ReplayView(template: nil, strategyID: strategyID) } label: {
                        Label("Replay a day", systemImage: "play.circle")
                    }
                    .disabled(Int(d["version"]?.number ?? 0) == 0)
                    NavigationLink { StrategyEditor(strategyID: strategyID) } label: {
                        Label(d["editing_draft"]?.bool == true ? "Continue editing" : "Edit", systemImage: "slider.horizontal.3")
                    }
                    Button("Backtest", systemImage: "chart.xyaxis.line") { showBacktest = true }
                        .disabled(Int(d["version"]?.number ?? 0) == 0)
                }
                Section {
                    Toggle("Include my last backtest's summary", isOn: $withResults)
                    Button("Make a share code", systemImage: "square.and.arrow.up") { Task { await makeShare() } }
                        .disabled(Int(d["version"]?.number ?? 0) == 0)
                    if let shareCode {
                        Text(shareCode).font(.caption2.monospaced()).lineLimit(3).textSelection(.enabled)
                        ShareLink(item: shareCode) { Label("Send the code", systemImage: "paperplane") }
                    }
                    Button("Export as a file", systemImage: "doc.badge.arrow.up") { Task { await export() } }
                        .disabled(Int(d["version"]?.number ?? 0) == 0)
                    if let exportText {
                        ShareLink(item: exportText, preview: SharePreview("\(d["name"]?.string ?? "strategy").json")) {
                            Label("Save or send the file", systemImage: "square.and.arrow.up.on.square")
                        }
                    }
                } header: { Text("Share") } footer: {
                    Text("A share carries the strategy's settings only: never your login, keys, account or positions. The friend runs it with their own lots and limits.")
                }
                Section("Versions") {
                    ForEach(Array((d["versions"]?.array ?? []).enumerated()), id: \.offset) { _, v in
                        HStack {
                            VStack(alignment: .leading) {
                                Text("v\(Int(v["version"]?.number ?? 0))").font(.headline)
                                if let n = v["note"]?.string, !n.isEmpty { Text(n).font(.caption) }
                                if let a = v["accepted_risk"]?.number {
                                    Text("Accepted \(Fmt.rupees(a, hidden: hide)) a trade").font(.caption).foregroundStyle(Color.tdWarn)
                                }
                            }
                            Spacer()
                            Button("Restore") { restoreVersion = Int(v["version"]?.number ?? 0) }.buttonStyle(.borderless).frame(minHeight: 44)
                        }
                    }
                }
                Section {
                    Button(d["archived"]?.bool == true ? "Unarchive" : "Archive", systemImage: "archivebox") { confirmArchive = true }
                }
            } else {
                ProgressView()
            }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle(doc?["name"]?.string ?? "Strategy")
        .task { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $showBacktest) {
            NavigationStack { StrategyBacktestForm(strategyID: strategyID) { showBacktest = false } }
        }
        .confirmationDialog("Restore version \(restoreVersion ?? 0) as the draft?",
                            isPresented: Binding(get: { restoreVersion != nil }, set: { if !$0 { restoreVersion = nil } }),
                            titleVisibility: .visible) {
            Button("Restore") { if let v = restoreVersion { Task { await restore(v) } } }
        } message: {
            Text("Your unsaved draft changes are replaced. Saved versions stay as they are; save to make it the newest.")
        }
        .confirmationDialog("Archive this strategy?", isPresented: $confirmArchive, titleVisibility: .visible) {
            Button("Archive") { Task { await archive() } }
        } message: { Text("It leaves My strategies; its versions and backtests are kept.") }
    }

    private func load() async {
        do { doc = try await model.api.raw("GET", "/v1/strategies/\(strategyID)"); message = nil }
        catch { message = error.localizedDescription }
    }

    private func makeShare() async {
        struct B: Encodable { let include_results: Bool }
        do { shareCode = try await model.api.raw("POST", "/v1/strategies/\(strategyID)/share", body: B(include_results: withResults))["code"]?.string }
        catch { message = error.localizedDescription }
    }

    private func export() async {
        do {
            let e = try await model.api.raw("GET", "/v1/strategies/\(strategyID)/export")
            let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted, .sortedKeys]
            exportText = String(data: try enc.encode(e), encoding: .utf8)
        } catch { message = error.localizedDescription }
    }

    private func restore(_ v: Int) async {
        do { message = try await model.api.raw("POST", "/v1/strategies/\(strategyID)/versions/\(v)/restore")["note"]?.string; await load() }
        catch { message = error.localizedDescription }
    }

    private func archive() async {
        do { _ = try await model.api.raw("POST", "/v1/strategies/\(strategyID)/archive"); await load() }
        catch { message = error.localizedDescription }
    }
}

struct StrategyBacktestForm: View {
    @Environment(AppModel.self) private var model
    let strategyID: Int
    var done: () -> Void
    @State private var start = Calendar.current.date(byAdding: .year, value: -1, to: .now) ?? .now
    @State private var end = Date.now
    @State private var honest = true
    @State private var message: String?

    var body: some View {
        Form {
            DatePicker("From", selection: $start, in: ...Date.now, displayedComponents: .date)
            DatePicker("To", selection: $end, in: start...Date.now, displayedComponents: .date)
            Toggle("Honest fills", isOn: $honest)
            Button("Run backtest") { Task { await run() } }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Backtest")
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { done() } } }
    }

    private func run() async {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = Fmt.ist
        struct B: Encodable { let start: String; let end: String; let fill_model: String }
        do {
            _ = try await model.api.raw("POST", "/v1/strategies/\(strategyID)/backtest",
                                        body: B(start: f.string(from: start), end: f.string(from: end), fill_model: honest ? "honest" : "classic"))
            done()
        } catch { message = error.localizedDescription }
    }
}

/// Paste a share code (from a friend or Algo Lab) or an exported file's text: see what it is and what changed.
struct ImportView: View {
    @Environment(AppModel.self) private var model
    var done: (Int?) -> Void
    @State private var code = ""
    @State private var preview: JSONValue?
    @State private var message: String?

    var body: some View {
        Form {
            Section("Share code or file text") {
                TextEditor(text: $code).font(.caption.monospaced()).frame(minHeight: 120)
                Button("Paste") { code = UIPasteboard.general.string ?? code }
                Button("Check") { Task { await check() } }.disabled(code.isEmpty)
            }
            if let p = preview {
                Section {
                    LabeledContent("Stop loss", value: stopText(p["stop_loss"]))
                    LabeledContent("Max loss a day", value: p["max_loss_per_day"]?.number.map { Fmt.rupees($0) } ?? "None set")
                } header: { Text("Its risk") } footer: {
                    Text("It imports as a draft. Saving it shows the full risk check against your capital first.")
                }
                Section("What it is") {
                    LabeledContent("Name", value: p["name"]?.string ?? "—")
                    LabeledContent("From", value: p["from"]?.string ?? "—")
                    LabeledContent("Kind", value: p["kind"]?.string == "python" ? "Python (Algo Lab)" : "No-code")
                    if let net = p["results.net_pnl"]?.number { LabeledContent("Their backtest", value: Fmt.rupees(net, signed: true)) }
                    if p["ok"]?.bool == false {
                        Label("Needs fixes before it can be saved; it imports as a draft.", systemImage: "exclamationmark.triangle")
                            .foregroundStyle(Color.tdWarn)
                    }
                }
                if let ex = p["existing"], ex != .null {
                    Section("You have it already: \(ex["name"]?.string ?? "")") {
                        let changes = p["changes"]?.array ?? []
                        if changes.isEmpty { Text("Nothing changed.") }
                        ForEach(Array(changes.enumerated()), id: \.offset) { _, c in
                            LabeledContent(c["path"]?.string ?? "", value: "\(show(c["from"])) → \(show(c["to"]))").font(.caption)
                        }
                        Button("Update my copy") { Task { await doImport("update") } }
                    }
                }
                Button("Import as a new strategy") { Task { await doImport("new") } }
            }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Import")
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { done(nil) } } }
    }

    private func stopText(_ v: JSONValue?) -> String {
        guard let v, v != .null, let val = v["value"]?.number else { return "None — required before saving" }
        let n = val.formatted(.number.precision(.fractionLength(0...2)))
        switch (v["on"]?.string, v["type"]?.string) {
        case ("underlying"?, _): return "Index \(n) pts (app-held)"
        case (_, "multiple"?): return "\(n)× premium (at Zerodha)"
        case (_, "percent"?): return "\(n)% of premium (at Zerodha)"
        default: return "\(n) pts of premium (at Zerodha)"
        }
    }

    private func show(_ v: JSONValue?) -> String {
        switch v {
        case .number(let n)?: return n.formatted()
        case .string(let s)?: return s
        case .bool(let b)?: return b ? "on" : "off"
        case .none, .null?: return "—"
        default: return "…"
        }
    }

    private func check() async {
        struct B: Encodable { let code: String }
        do { preview = try await model.api.raw("POST", "/v1/strategies/import/preview", body: B(code: code)); message = nil }
        catch { message = error.localizedDescription; preview = nil }
    }

    private func doImport(_ mode: String) async {
        struct B: Encodable { let code: String; let mode: String }
        do {
            let r = try await model.api.raw("POST", "/v1/strategies/import", body: B(code: code, mode: mode))
            done(r["id"]?.number.map { Int($0) })
        } catch { message = error.localizedDescription }
    }
}


/// Research -> paper -> shadow -> live, with the checks for the next stage.
struct StageSection: View {
    @Environment(AppModel.self) private var model
    let strategyID: Int
    @State private var g: JSONValue?
    @State private var message: String?
    @State private var confirmPromote = false
    @State private var sending = false
    @State private var showDeploy = false

    var body: some View {
        Section {
            if let g {
                LabeledContent("Stage", value: humanize(g["stage"]?.string ?? "research"))
                ForEach(Array((g["checks"]?.array ?? []).enumerated()), id: \.offset) { _, c in
                    Label {
                        VStack(alignment: .leading) {
                            Text(c["name"]?.string ?? "").font(.subheadline)
                            Text(c["reason"]?.string ?? "").font(.caption).foregroundStyle(.secondary)
                        }
                    } icon: {
                        Image(systemName: c["ok"]?.bool == true ? "checkmark.circle.fill" : "xmark.circle")
                            .foregroundStyle(c["ok"]?.bool == true ? Color.tdGood : Color.tdBad)
                    }
                }
                ForEach((g["advice"]?.array ?? []).compactMap(\.string), id: \.self) { a in
                    Label(a, systemImage: "lightbulb").font(.caption).foregroundStyle(Color.tdWarn)
                }
                if ["paper", "shadow", "live"].contains(g["stage"]?.string ?? "") {
                    Button("Start a paper session", systemImage: "play.circle") { showDeploy = true }
                }
                if g["can_promote"]?.bool == true {
                    Button { confirmPromote = true } label: {
                        HStack { Label("Move to \(humanize(g["next"]?.string ?? "paper"))", systemImage: "arrow.right.circle"); if sending { Spacer(); ProgressView() } }
                    }
                    .disabled(sending)
                } else {
                    Text(g["note"]?.string ?? "").font(.caption).foregroundStyle(.secondary)
                }
            } else if message == nil { ProgressView() }
            if let message {
                Text(message).font(.footnote)
                if g == nil { Button("Retry") { Task { await load() } } }
            }
        } header: { Text("Stage") }
        .task { await load() }
        .sheet(isPresented: $showDeploy) {
            NavigationStack {
                DeploySheet(strategyID: strategyID) { id in
                    showDeploy = false
                    if id != nil { model.tab = .trade }
                }
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { showDeploy = false } } }
            }
        }
        .confirmationDialog("Move to \(humanize(g?["next"]?.string ?? "paper"))?", isPresented: $confirmPromote, titleVisibility: .visible) {
            Button("Move") { Task { await promote() } }
        } message: {
            Text("All \((g?["checks"]?.array ?? []).count) checks passed. It runs the saved version with its stop loss and max loss a day as saved. Paper places no real orders.")
        }
    }

    private func load() async {
        do { g = try await model.api.raw("GET", "/v1/strategies/\(strategyID)/stage"); message = nil }
        catch { message = "Couldn't load the stage: \(error.localizedDescription)" }
    }
    private func promote() async {
        sending = true; defer { sending = false }
        do { g = try await model.api.raw("POST", "/v1/strategies/\(strategyID)/stage/promote") } catch { message = error.localizedDescription }
    }
}

/// Presets: named settings on top of the saved version; backtest one with a number of lots.
struct PresetsSection: View {
    @Environment(AppModel.self) private var model
    let strategyID: Int
    let saved: Bool
    @State private var presets: [JSONValue] = []
    @State private var newName = ""
    @State private var message: String?
    @State private var lots: [Int: Int] = [:]          // per preset row
    @State private var busy: Int?
    @State private var pending: PendingAccept?

    var body: some View {
        Section {
            ForEach(Array(presets.enumerated()), id: \.offset) { _, p in
                let pid = Int(p["id"]?.number ?? 0)
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(p["name"]?.string ?? "").font(.headline)
                        if p["is_default"]?.bool == true { StatusPill(text: "Default", systemImage: "star", color: .tdPaper) }
                    }
                    Text((p["overrides"]?.object ?? [:]).map { "\(humanize($0.key.components(separatedBy: ".").suffix(2).joined(separator: " "))) \($0.value.number.map { $0.formatted() } ?? $0.value.string ?? "…")" }
                        .sorted().joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
                    Stepper("Lots \(lots[pid] ?? 1)", value: Binding(get: { lots[pid] ?? 1 }, set: { lots[pid] = $0 }), in: 1...20)
                        .frame(minHeight: 44)
                    Button {
                        Task { await backtest(pid) }
                    } label: {
                        HStack { Text("Backtest 1 year"); if busy == pid { ProgressView() } }.frame(minHeight: 44)
                    }
                    .buttonStyle(.borderless).disabled(busy != nil)
                }
                .swipeActions { Button("Delete", role: .destructive) { Task { await delete(pid) } } }
            }
            if saved {
                HStack {
                    TextField("New preset from the saved version", text: $newName)
                    Button("Add") { Task { await add() } }.disabled(newName.isEmpty).buttonStyle(.borderless)
                }
            }
            if let message { Text(message).font(.footnote) }
        } header: { Text("Presets") } footer: {
            Text("Save a preset from a backtest's settings with Save as preset on its result, or here from the saved version.")
        }
        .task { await load() }
        .sheet(item: $pending) { RiskAcceptSheet(pending: $0) }
    }

    private func load() async {
        do { presets = try await model.api.raw("GET", "/v1/strategies/\(strategyID)/presets").array ?? []; message = nil }
        catch { message = "Couldn't load presets: \(error.localizedDescription) Pull to refresh to retry." }
    }

    private func add(accept: Double? = nil) async {
        var body: [String: JSONValue] = ["name": .string(newName), "overrides": .object([:])]
        if let accept { body["accept_risk"] = .number(accept) }
        do {
            _ = try await model.api.raw("POST", "/v1/strategies/\(strategyID)/presets", body: JSONValue.object(body))
            newName = ""; await load()
        } catch {
            if let p = PendingAccept.from(error, retry: { await add(accept: $0) }) { pending = p }
            else { message = error.localizedDescription }
        }
    }

    private func delete(_ id: Int) async {
        _ = try? await model.api.raw("DELETE", "/v1/strategies/\(strategyID)/presets/\(id)"); await load()
    }

    private func backtest(_ id: Int) async {
        busy = id; defer { busy = nil }
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = Fmt.ist
        let start = Calendar.current.date(byAdding: .year, value: -1, to: .now) ?? .now
        do {
            _ = try await model.api.raw("POST", "/v1/strategies/\(strategyID)/presets/\(id)/backtest",
                                        body: JSONValue.object(["start": .string(f.string(from: start)), "end": .string(f.string(from: .now)),
                                                                "lots": .number(Double(lots[id] ?? 1))]))
            message = "Backtest queued: see Strategies › Backtests."
        } catch { message = error.localizedDescription }
    }
}
