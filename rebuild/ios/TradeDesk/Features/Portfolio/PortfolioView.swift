import Charts
import SwiftUI
import UniformTypeIdentifiers

/// Home card: India portfolio value and today's change, plus US stocks in rupees when "Include US" is on
/// (remembered on this device).
struct NetWorthCard: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    @AppStorage("includeUS") private var includeUS = true
    @State private var data: JSONValue?
    @State private var failed = false

    var body: some View {
        GlassCard {
            CardHeader(title: "Net worth", systemImage: "chart.pie", trailing: updatedText)
            if failed && data == nil {
                Text("Couldn't load the portfolio.").font(.footnote).foregroundStyle(.secondary)
                Button("Retry") { Task { await load() } }.buttonStyle(.glass)
            } else if let d = data {
                let india = d["india"]?["value"]?.number ?? 0
                let us = includeUS ? (d["us"]?["value_inr"]?.number ?? 0) : 0
                let day = (d["india"]?["day_pnl"]?.number ?? 0) + (includeUS ? (d["us"]?["day_pnl_inr"]?.number ?? 0) : 0)
                let pct = (india + us - day) != 0 ? day / (india + us - day) * 100 : 0
                let label = d["india"]?["day_of"]?.string != nil ? "Last day" : "Today"
                if india == 0 && us == 0 {
                    Text("No holdings yet. They appear once the server has synced with Zerodha.").font(.footnote).foregroundStyle(.secondary)
                } else {
                    // the % first: it says the most at a glance, and it is all a login with hidden amounts can see
                    HStack(alignment: .firstTextBaseline) {
                        Image(systemName: pct > 0 ? "arrow.up.right" : pct < 0 ? "arrow.down.right" : "equal")
                            .foregroundStyle(pct > 0 ? Color.tdGood : pct < 0 ? Color.tdBad : .secondary)
                        Text(String(format: "%+.2f%% %@", pct, label.lowercased())).font(.title2.weight(.semibold)).monospacedDigit()
                            .foregroundStyle(pct > 0 ? Color.tdGood : pct < 0 ? Color.tdBad : .primary)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Net worth \(pct >= 0 ? "up" : "down") \(String(format: "%.2f", abs(pct))) percent \(label.lowercased())")
                    if !hide {
                        Text(Fmt.rupeesShort(india + us)).font(.title.weight(.semibold)).monospacedDigit()
                        PnlLine(label: label, value: day, mode: "Portfolio")
                    }
                }
                HStack {
                    NavigationLink(value: Route.portfolio) {
                        Text(hide ? "India portfolio" : "India " + Fmt.rupeesShort(india)).font(.caption).frame(minHeight: 44)
                    }
                    Spacer()
                    if let u = d["us"], u != .null {
                        Toggle("Include US", isOn: $includeUS).font(.caption).fixedSize()
                    }
                }
                if includeUS, d["us"]?["stale"]?.bool == true {
                    Label("US figures are stale", systemImage: "clock.badge.exclamationmark").font(.caption2).foregroundStyle(Color.tdWarn)
                }
            } else {
                ProgressView()
            }
        }
        .task(id: model.lastRefresh) {          // with Home's pull to refresh, and every 60 s while on screen
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    private var updatedText: String? {
        guard let u = data?["india"]?["updated"]?.string else { return nil }
        let t = Fmt.storeTime(u)
        let stale = (Fmt.parseIST(u).map { Date.now.timeIntervalSince($0) > 180 } ?? false)
            && model.status?.market.state == "open"
        return (stale ? "Stale · " : "Updated ") + t
    }

    private func load() async {
        do { data = try await model.api.raw("GET", "/v1/portfolio/networth"); failed = false } catch { failed = true }
    }
}

extension Fmt {
    static func rupeesShortHidden(_ v: Double, hidden: Bool) -> String { hidden ? "₹ ••••" : rupeesShort(v) }
}

/// More › Portfolio › India: the owner's Zerodha holdings with Day / Total, sort, best and worst of the day,
/// charts (today, by day, total with its high–low band), recent alerts, pause sync, tradebook import and CSV export.
struct PortfolioView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    @AppStorage("pfMode") private var mode = "day"          // day | total
    @AppStorage("pfSort") private var sort = "value"        // value | move | pnl | name
    @AppStorage("pfChart") private var chart = "today"      // today | days | total
    @State private var data: JSONValue?
    @State private var history: [JSONValue] = []
    @State private var error: String?
    @State private var confirmPause = false
    @State private var importing = false
    @State private var note: String?
    @State private var exportURL: URL?
    @State private var showSettings = false
    @State private var historyFailed = false

    var body: some View {
        List {
            if let error {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn)
                    Button("Retry") { Task { await load() } }
                }
            }
            if let note { Section { Label(note, systemImage: "info.circle").font(.footnote) } }
            if let d = data {
                statusSection(d)
                summarySection(d)
                chartSection(d)
                holdingsSection(d)
                alertsSection(d)
                actionsSection(d)
            } else if error == nil {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("India portfolio")
        .refreshable { await load() }
        .task {
            while !Task.isCancelled {                 // every 60 s while open; the server syncs each minute
                await load()
                try? await Task.sleep(for: .seconds(60))
            }
        }
        .task(id: chart) { if chart == "total" { await loadHistory() } }
        .confirmationDialog(data?["sync_paused"]?.bool == true ? "Resume portfolio sync?" : "Pause portfolio sync?",
                            isPresented: $confirmPause, titleVisibility: .visible) {
            Button(data?["sync_paused"]?.bool == true ? "Resume sync" : "Pause sync") { Task { await togglePause() } }
        } message: { Text(pauseMessage) }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.commaSeparatedText, .spreadsheet, .data]) { r in
            if case .success(let url) = r { Task { await upload(url) } }
        }
        .sheet(isPresented: $showSettings) { PortfolioAlertSettings { showSettings = false } }
    }

    private var pauseMessage: String {
        if data?["sync_paused"]?.bool == true { return "The server talks to Zerodha again in the sync hours (08:45–16:00 IST on trading days)." }
        return data?["market_open"]?.bool == true
            ? "While paused the figures go stale, portfolio alerts stop, and if still paused after the close today's closing figures and trades are not recorded."
            : "No contact with Zerodha until you resume. Nothing is missed while the market is closed."
    }

    @ViewBuilder private func statusSection(_ d: JSONValue) -> some View {
        Section {
            HStack {
                if d["sync_paused"]?.bool == true {
                    StatusPill(text: "Sync paused", systemImage: "pause.fill", color: .tdWarn)
                } else if d["logged_in"]?.bool == false {
                    StatusPill(text: model.user?.role == "owner" ? "Zerodha login needed" : "Waiting for the owner's Zerodha login",
                               systemImage: "key", color: .tdWarn)
                } else if let day = d["day_of"]?.string {
                    StatusPill(text: "Closed · \(Fmt.marketDay(day))", systemImage: "moon", color: .secondary)
                } else if d["market_open"]?.bool == true, let age = ageMinutes(d), age > 3 {
                    StatusPill(text: "Stale · \(age) min old", systemImage: "clock.badge.exclamationmark", color: .tdWarn)
                } else if d["market_open"]?.bool == true {
                    StatusPill(text: "Live", systemImage: "dot.radiowaves.left.and.right", color: .tdGood)
                }
                if d["amounts_hidden"]?.bool == true {
                    StatusPill(text: "Amounts hidden", systemImage: "eye.slash", color: .secondary)
                }
                Spacer()
                if let u = d["updated"]?.string { Text("Updated \(Fmt.storeTime(u))").font(.caption).foregroundStyle(.secondary) }
            }
            if let e = d["error"]?.string { Label(e, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Color.tdWarn) }
            if d["logged_in"]?.bool == false && model.user?.role == "owner" {
                KiteLoginButton()
            }
        }
    }

    private func ageMinutes(_ d: JSONValue) -> Int? {
        guard let u = d["updated"]?.string, let t = Fmt.parseIST(u) else { return nil }
        return Int(Date.now.timeIntervalSince(t) / 60)
    }

    private var hiddenView: Bool { data?["amounts_hidden"]?.bool == true }

    private func money(_ v: Double?, signed: Bool = false) -> String {
        guard let v else { return "—" }
        return Fmt.rupees(v, hidden: hide, signed: signed)
    }

    private func pctText(_ v: Double?) -> String {
        guard let v else { return "—" }
        return (v > 0 ? "+" : v < 0 ? "−" : "") + String(format: "%.2f%%", abs(v))
    }

    private func summarySection(_ d: JSONValue) -> some View {
        let s = d["summary"]
        return Section {
            VStack(alignment: .leading, spacing: 6) {
                Text(money(s?["value"]?.number)).font(.largeTitle.weight(.semibold)).monospacedDigit()
                    .accessibilityLabel("Value \(money(s?["value"]?.number))")
                PnlLine(label: (d["day_of"]?.string != nil ? "Last day " : "Today ") + pctText(s?["day_pct"]?.number),
                        value: s?["day_pnl"]?.number ?? 0, big: true, mode: "Portfolio")
                PnlLine(label: "Total " + pctText(s?["pnl_pct"]?.number), value: s?["pnl"]?.number ?? 0, mode: "Portfolio")
            }
            LabeledContent("Invested", value: money(s?["invested"]?.number))
            LabeledContent("Gold", value: "\(money(s?["gold_value"]?.number)) · \(String(format: "%.1f%%", s?["gold_pct"]?.number ?? 0))")
            LabeledContent("Up · down today", value: "\(Int(s?["up"]?.number ?? 0)) up · \(Int(s?["down"]?.number ?? 0)) down")
            if let x = d["extremes"]?["day"] {
                LabeledContent("Today's range") {
                    VStack(alignment: .trailing, spacing: 2) {
                        Text("High \(money(x["high"]?.number, signed: true)) at \(String((x["high_ts"]?.string ?? "").suffix(8).prefix(5))) IST")
                        Text("Low \(money(x["low"]?.number, signed: true)) at \(String((x["low_ts"]?.string ?? "").suffix(8).prefix(5))) IST")
                    }
                    .font(.caption).monospacedDigit()
                }
            }
        }
    }

    @ViewBuilder private func chartSection(_ d: JSONValue) -> some View {
        Section {
            Picker("Chart", selection: $chart) {
                Text("Today").tag("today"); Text("By day").tag("days"); Text("Total").tag("total")
            }
            .pickerStyle(.segmented)
            Group {
                switch chart {
                case "days":
                    let rows = Array((d["daily"]?.array ?? []).suffix(60))
                    Chart(Array(rows.enumerated()), id: \.offset) { _, r in
                        BarMark(x: .value("Day", r["day"]?.string ?? ""), y: .value("Day P&L", r["day_pnl"]?.number ?? 0))
                            .foregroundStyle((r["day_pnl"]?.number ?? 0) >= 0 ? Color.tdGood : Color.tdBad)
                    }
                    .chartXAxis(.hidden)
                case "total":
                    Chart(Array(history.enumerated()), id: \.offset) { _, r in  // (empty: see the overlay)
                        AreaMark(x: .value("Day", r["day"]?.string ?? ""), yStart: .value("Low", r["pnl_low"]?.number ?? 0),
                                 yEnd: .value("High", r["pnl_high"]?.number ?? 0))
                            .foregroundStyle(Color.accentColor.opacity(0.2))
                        LineMark(x: .value("Day", r["day"]?.string ?? ""), y: .value("Total P&L", r["pnl"]?.number ?? 0))
                    }
                    .chartXAxis(.hidden)
                    .overlay { if history.isEmpty { Text(historyFailed ? "Couldn't load the history. Pull to retry." : "Loading…").font(.footnote).foregroundStyle(.secondary) } }
                default:
                    let rows = d["intraday"]?.array ?? []
                    Chart {
                        ForEach(Array(rows.enumerated()), id: \.offset) { _, r in
                            LineMark(x: .value("Time", r["t"]?.string ?? ""), y: .value("Day P&L", r["day_pnl"]?.number ?? 0))
                        }
                        RuleMark(y: .value("Zero", 0)).foregroundStyle(.secondary)
                    }
                    .overlay { if rows.isEmpty { Text("No figures yet today.").font(.footnote).foregroundStyle(.secondary) } }
                    .chartXAxis { AxisMarks(values: .automatic(desiredCount: 4)) }
                }
            }
            .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if !hide, let x = v.as(Double.self) { Text(Fmt.rupeesShort(x)) } } } }
            .frame(height: 200)
            .accessibilityLabel(chart == "today" ? "Day P&L through the day" : chart == "days" ? "Day P&L by day, last 60 days" : "Total P&L by day with the day's high and low")
        } footer: {
            Text(chart == "total" ? "Line: total P&L at each day's last figure. Band: that day's high and low." : chart == "days" ? "Green: up days. Red: down days." : "")
        }
    }

    private func sorted(_ rows: [JSONValue]) -> [JSONValue] {
        let key: (JSONValue) -> Double = { r in
            switch sort {
            case "move": return mode == "day" ? (r["day_pct"]?.number ?? 0) : (r["pnl_pct"]?.number ?? 0)
            case "pnl": return mode == "day" ? (r["day_pnl"]?.number ?? 0) : (r["pnl"]?.number ?? 0)
            default: return r["value"]?.number ?? 0
            }
        }
        if sort == "name" { return rows.sorted { ($0["symbol"]?.string ?? "") < ($1["symbol"]?.string ?? "") } }
        return rows.sorted { key($0) > key($1) }
    }

    @ViewBuilder private func holdingsSection(_ d: JSONValue) -> some View {
        let rows = d["holdings"]?.array ?? []
        if rows.count >= 2 {
            let byMove = rows.sorted { ($0["day_pct"]?.number ?? 0) > ($1["day_pct"]?.number ?? 0) }
            Section("Best and worst today") {
                moverRow(byMove.first!, label: "Best")
                moverRow(byMove.last!, label: "Worst")
            }
        }
        Section {
            HStack {
                Picker("Show", selection: $mode) { Text("Day").tag("day"); Text("Total").tag("total") }
                    .pickerStyle(.segmented).frame(maxWidth: 160)
                Spacer()
                Menu {
                    Picker("Sort by", selection: $sort) {
                        Text("Value").tag("value"); Text("Move %").tag("move"); Text("P&L").tag("pnl"); Text("Name").tag("name")
                    }
                } label: { Label("Sort", systemImage: "arrow.up.arrow.down").frame(minHeight: 44) }
            }
            if rows.isEmpty { Text("No holdings yet.").foregroundStyle(.secondary) }
            ForEach(Array(sorted(rows).enumerated()), id: \.offset) { _, h in
                NavigationLink(value: Route.portfolioStock(h["symbol"]?.string ?? "")) { holdingRow(h) }
            }
        } header: { Text("\(rows.count) holdings") }
    }

    private func moverRow(_ h: JSONValue, label: String) -> some View {
        let p = h["day_pct"]?.number ?? 0
        return HStack {
            Text(label).font(.caption).foregroundStyle(.secondary).fixedSize()
            Text(h["symbol"]?.string ?? "").font(.subheadline.weight(.semibold))
            Spacer()
            Text((p > 0 ? "▲ " : p < 0 ? "▼ " : "") + pctText(p)).monospacedDigit()
                .foregroundStyle(p > 0 ? Color.tdGood : p < 0 ? Color.tdBad : .primary)
            if !hiddenView {
                Text(money(h["day_pnl"]?.number, signed: true)).font(.caption).monospacedDigit().foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private func holdingRow(_ h: JSONValue) -> some View {
        if hiddenView { hiddenRow(h) } else { fullRow(h) }
    }

    /// Amounts hidden by the owner: the server sends only % moves and a whole-percent weight.
    private func hiddenRow(_ h: JSONValue) -> some View {
        let p = (mode == "day" ? h["day_pct"]?.number : h["pnl_pct"]?.number) ?? 0
        return HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(h["symbol"]?.string ?? "").font(.subheadline.weight(.semibold))
                Text("\(Int(h["weight_pct"]?.number ?? 0))% of the portfolio").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Text((p > 0 ? "▲ " : p < 0 ? "▼ " : "") + pctText(p)).monospacedDigit()
                .foregroundStyle(p > 0 ? Color.tdGood : p < 0 ? Color.tdBad : .primary)
        }
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(h["symbol"]?.string ?? ""), \(mode == "day" ? "today" : "total") \(p >= 0 ? "up" : "down") \(String(format: "%.1f", abs(p))) percent")
    }

    private func fullRow(_ h: JSONValue) -> some View {
        let day = mode == "day"
        let pnl = day ? (h["day_pnl"]?.number ?? 0) : (h["pnl"]?.number ?? 0)
        let pct = day ? h["day_pct"]?.number : h["pnl_pct"]?.number
        return HStack {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(h["symbol"]?.string ?? "").font(.subheadline.weight(.semibold))
                    if h["asset"]?.string == "Gold" { Image(systemName: "seal.fill").font(.caption2).foregroundStyle(Color.tdWarn).accessibilityLabel("Gold") }
                }
                Text(hide || h["qty"]?.number == nil ? "LTP \(num(h["last_price"]?.number))"
                     : "\(num(h["qty"]?.number, 0)) × \(num(h["last_price"]?.number)) · \(String(format: "%.1f", h["weight"]?.number ?? 0))%")
                    .font(.caption).foregroundStyle(.secondary).monospacedDigit()
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(money(pnl, signed: true)).monospacedDigit().foregroundStyle(pnl > 0 ? Color.tdGood : pnl < 0 ? Color.tdBad : .primary)
                Text(pctText(pct)).font(.caption).monospacedDigit().foregroundStyle(.secondary)
            }
        }
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(h["symbol"]?.string ?? ""), \(day ? "today" : "total") \(pnl >= 0 ? "profit" : "loss") \(money(abs(pnl))), \(pctText(pct))")
    }

    @ViewBuilder private func alertsSection(_ d: JSONValue) -> some View {
        let a = d["alerts"]?.array ?? []
        if !a.isEmpty {
            Section("Recent portfolio alerts") {
                ForEach(Array(a.prefix(10).enumerated()), id: \.offset) { _, x in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(x["message"]?.string ?? "").font(.subheadline)
                        Text(Fmt.storeTime(x["ts"]?.string ?? "")).font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    @ViewBuilder private func actionsSection(_ d: JSONValue) -> some View {
        Section {
            if d["can_use"]?.bool == true {
                Button(d["sync_paused"]?.bool == true ? "Resume sync" : "Pause sync",
                       systemImage: d["sync_paused"]?.bool == true ? "play.fill" : "pause.fill") { confirmPause = true }
                Button("Import a tradebook (from Console)", systemImage: "square.and.arrow.down") { importing = true }
            }
            if d["amounts_hidden"]?.bool != true {
                if let exportURL {
                    ShareLink(item: exportURL) { Label("Share history (CSV)", systemImage: "square.and.arrow.up") }
                } else {
                    Button("Download history (CSV)", systemImage: "tablecells") { Task { await export() } }
                }
            }
            if model.user?.role == "owner" {
                Button("Portfolio alerts and gold", systemImage: "bell.badge") { showSettings = true }
            }
        } footer: {
            Text("Read-only: the server only reads holdings, trades and prices from Zerodha, 08:45–16:00 IST on trading days. A tradebook import fills in the history Zerodha's API does not keep; importing the same file again adds nothing.")
        }
    }

    private func load() async {
        do { data = try await model.api.raw("GET", "/v1/portfolio"); error = nil }
        catch { self.error = "Couldn't load the portfolio: \(error.localizedDescription)" }
    }

    private func loadHistory() async {
        do {
            history = try await model.api.raw("GET", "/v1/portfolio/history")["rows"]?.array ?? []
            historyFailed = false
        } catch { historyFailed = true }
    }

    private func togglePause() async {
        let paused = !(data?["sync_paused"]?.bool ?? false)
        do {
            _ = try await model.api.raw("POST", "/v1/portfolio/sync", body: JSONValue.object(["paused": .bool(paused)]))
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func upload(_ url: URL) async {
        let ok = url.startAccessingSecurityScopedResource()
        defer { if ok { url.stopAccessingSecurityScopedResource() } }
        guard let bytes = try? Data(contentsOf: url) else { error = "Couldn't read that file."; return }
        do {
            let r = try await model.api.raw("POST", "/v1/portfolio/tradebook", body: JSONValue.object([
                "name": .string(url.lastPathComponent), "data_b64": .string(bytes.base64EncodedString())]))
            note = r["message"]?.string
        } catch { self.error = error.localizedDescription }
    }

    private func export() async {
        do {
            let bytes = try await model.api.download("/v1/portfolio/export.csv")
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("portfolio-history.csv")
            try bytes.write(to: url)
            exportURL = url
        } catch { self.error = error.localizedDescription }
    }
}

/// Owner: alert thresholds and which symbols count as gold.
struct PortfolioAlertSettings: View {
    @Environment(AppModel.self) private var model
    var done: () -> Void
    @State private var holding = 3.0
    @State private var portfolio = 2.0
    @State private var bandLo = 5.0
    @State private var bandHi = 25.0
    @State private var gold = ""
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Alert once a day when") {
                    Stepper("A holding moves \(num(holding, 1))%", value: $holding, in: 0...20, step: 0.5)
                    Stepper("The portfolio moves \(num(portfolio, 1))%", value: $portfolio, in: 0...10, step: 0.5)
                    Stepper("Gold below \(Int(bandLo))%", value: $bandLo, in: 0...(bandHi - 1), step: 1)
                    Stepper("Gold above \(Int(bandHi))%", value: $bandHi, in: (bandLo + 1)...100, step: 1)
                }
                Section {
                    TextField("e.g. GOLDBEES, SGBJUN31", text: $gold).textInputAutocapitalization(.characters)
                } header: { Text("Also count as gold") } footer: {
                    Text("Symbols with GOLD in the name count already. 0 turns an alert off.")
                }
                if let error { Text(error).foregroundStyle(Color.tdBad) }
            }
            .navigationTitle("Portfolio alerts")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { done() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { Task { await save() } }.disabled(!loaded) }
            }
            .task { await load() }
        }
    }

    private func load() async {
        guard let s = try? await model.api.raw("GET", "/v1/portfolio/settings") else {
            error = "Couldn't load your settings. Close and try again."
            return
        }
        loaded = true
        holding = s["alerts"]?["holding_day_move_pct"]?.number ?? 0
        portfolio = s["alerts"]?["portfolio_day_move_pct"]?.number ?? 0
        let band = s["alerts"]?["gold_allocation_band_pct"]?.array?.compactMap(\.number) ?? [5, 25]
        if band.count == 2 { bandLo = band[0]; bandHi = band[1] }
        gold = (s["gold_symbols"]?.array ?? []).compactMap(\.string).joined(separator: ", ")
    }

    private func save() async {
        let syms = gold.split(separator: ",").map { JSONValue.string($0.trimmingCharacters(in: .whitespaces)) }
        let body: JSONValue = .object(["holding_day_move_pct": .number(holding), "portfolio_day_move_pct": .number(portfolio),
                                       "gold_allocation_band_pct": .array([.number(bandLo), .number(bandHi)]),
                                       "gold_symbols": .array(syms)])
        do { _ = try await model.api.raw("PUT", "/v1/portfolio/settings", body: body); done() }
        catch { self.error = error.localizedDescription }
    }
}
