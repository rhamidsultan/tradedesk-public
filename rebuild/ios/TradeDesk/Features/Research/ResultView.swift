import Charts
import SwiftUI

/// One backtest's result: net after charges first, then curves, breakdowns, robustness, data used, trades.
struct ResultView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    let runID: Int
    @State private var detail: BacktestDetail?
    @State private var curves: [CurveDay] = []
    @State private var trades: [TradeItem] = []
    @State private var totalTrades = 0
    @State private var error: String?
    @State private var section = 0
    @State private var confirmDelete = false
    @State private var showSweep = false
    @State private var pendingAccept: PendingAccept?
    @State private var presetSaved = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        List {
            if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            if let d = detail {
                if d.run.status != "done" {
                    Section {
                        Label(statusText(d.run), systemImage: "hourglass")
                        if d.run.status == "running" { ProgressView(value: Double(d.run.progress), total: 100) }
                        if !d.run.note.isEmpty { Text(d.run.note).font(.caption).foregroundStyle(.secondary) }
                        if d.run.status == "failed" || d.run.status == "cancelled" {
                            Button("Run again", systemImage: "arrow.clockwise") { Task { await rerun() } }
                        }
                    }
                } else if let m = d.metrics {
                    headline(m)
                    Picker("Show", selection: $section) {
                        Text("Curves").tag(0); Text("Breakdowns").tag(1); Text("Risk").tag(2); Text("Data").tag(3)
                    }
                    .pickerStyle(.segmented)
                    .listRowBackground(Color.clear)
                    switch section {
                    case 0: curveSections
                    case 1: breakdowns(m)
                    case 2: risk(m)
                    default: if let a = d.audit { audit(a, m) }
                    }
                    tradesSection
                }
            } else if error == nil {
                ProgressView()
            } else {
                Button("Retry") { Task { await load() } }
            }
        }
        .navigationTitle(detail?.run.name ?? "Result")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            Menu {
                Button("Run again", systemImage: "arrow.clockwise") { Task { await rerun() } }
                Button("Optimise (find best settings)", systemImage: "slider.horizontal.below.square.and.square.filled") { showSweep = true }
                if let sid = detail?.run.strategyId {
                    Button("Save as preset", systemImage: "bookmark") { Task { await savePreset(sid) } }
                }
                Button("Delete", systemImage: "trash", role: .destructive) { confirmDelete = true }
            } label: { Image(systemName: "ellipsis.circle") }
        }
        .navigationDestination(isPresented: $showSweep) { SweepFormView(template: nil, specFromRun: runID) }
        .confirmationDialog("Delete this run?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { Task { await delete() } }
        }
        .task { await load() }
        .refreshable { await load() }
        .sheet(item: $pendingAccept) { RiskAcceptSheet(pending: $0) }
        .alert("Saved as a preset", isPresented: $presetSaved) { Button("OK") {} } message: {
            Text("Find it on the strategy's page under Presets.")
        }
    }

    // MARK: sections

    private func headline(_ m: Metrics) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                Text("Net after charges").font(.caption).foregroundStyle(.secondary)
                Text(Fmt.rupees(m.netPnl, hidden: hide, signed: true))
                    .font(.largeTitle.bold().monospacedDigit())
                    .foregroundStyle(m.netPnl >= 0 ? Color.tdGood : Color.tdBad)
                Text("Gross \(Fmt.rupees(m.grossPnl, hidden: hide, signed: true)) − charges \(Fmt.rupees(m.charges, hidden: hide))"
                     + (m.chargesPctOfGross.map { " (\(Int($0))% of gross)" } ?? ""))
                    .font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                if detail?.run.fillModel == "honest" {
                    StatusPill(text: "Honest fills", systemImage: "checkmark.shield", color: .tdGood)
                } else {
                    StatusPill(text: "Classic fills: optimistic, for comparison only", systemImage: "exclamationmark.triangle",
                               color: .tdWarn)
                }
            }
            .accessibilityElement(children: .combine)
            if m.luckWarning == true {
                Label("Too few trades, or a few trades make most of the profit: treat this as luck until more data agrees.",
                      systemImage: "dice").font(.footnote).foregroundStyle(Color.tdWarn)
            }
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                stat("Max drawdown", Fmt.rupees(-m.maxDrawdown, hidden: hide))
                stat("Trades", "\(m.trades)")
                stat("Win rate", m.winRatePct.map { "\(Int($0))%" } ?? "—")
                stat("Expectancy", m.expectancy.map { Fmt.rupees($0, hidden: hide) } ?? "—")
                stat("Avg win", m.avgWin.map { Fmt.rupees($0, hidden: hide) } ?? "—")
                stat("Avg loss", m.avgLoss.map { Fmt.rupees($0, hidden: hide) } ?? "—")
            }
        }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(spacing: 2) {
            Text(value).font(.subheadline.weight(.semibold).monospacedDigit()).lineLimit(1).minimumScaleFactor(0.7)
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var curveSections: some View {
        Section("Equity") {
            Chart(curves) { d in
                if let t = Fmt.parseIST(d.date) { LineMark(x: .value("Day", t), y: .value("Equity", d.equity)) }
            }
            .chartYScale(domain: .automatic(includesZero: false))
            .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if let x = v.as(Double.self) { Text(hide ? "••" : Fmt.rupeesShort(x)) } } } }
            .frame(height: 180)
            .accessibilityLabel(curveSummary)
        }
        Section("Drawdown") {
            Chart(curves) { d in
                if let t = Fmt.parseIST(d.date) {
                    AreaMark(x: .value("Day", t), y: .value("Drawdown", d.drawdown)).foregroundStyle(Color.tdBad.opacity(0.5))
                }
            }
            .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if let x = v.as(Double.self) { Text(hide ? "••" : Fmt.rupeesShort(x)) } } } }
            .frame(height: 120)
        }
        Section("P&L by close day") { PnlCalendar(days: curves, hide: hide) }
    }

    @ViewBuilder private func breakdowns(_ m: Metrics) -> some View {
        Section("By year") {
            ForEach(m.byYear.keys.sorted(), id: \.self) { y in money(y, m.byYear[y] ?? 0) }
        }
        if let w = m.byWeekday {
            Section("By weekday") { ForEach(["Mon", "Tue", "Wed", "Thu", "Fri"].filter { w[$0] != nil }, id: \.self) { money($0, w[$0] ?? 0) } }
        }
        if let r = m.byExitReason {
            Section("By exit reason") {
                ForEach(r.keys.sorted(by: { (r[$0]?.net ?? 0) < (r[$1]?.net ?? 0) }), id: \.self) { k in
                    LabeledContent("\(humanize(k)) · \(r[k]?.trades ?? 0)") {
                        Text(Fmt.rupees(r[k]?.net ?? 0, hidden: hide, signed: true)).monospacedDigit()
                            .foregroundStyle((r[k]?.net ?? 0) >= 0 ? Color.tdGood : Color.tdBad)
                    }
                }
            }
        }
        if let bm = m.byMonth {
            Section("By month") { ForEach(bm.keys.sorted(by: >), id: \.self) { money(Fmt.month($0), bm[$0] ?? 0) } }
        }
    }

    private func money(_ label: String, _ v: Double) -> some View {
        LabeledContent(label) {
            Text(Fmt.rupees(v, hidden: hide, signed: true)).monospacedDigit().foregroundStyle(v >= 0 ? Color.tdGood : Color.tdBad)
        }
    }

    @ViewBuilder private func risk(_ m: Metrics) -> some View {
        Section("Ratios") {
            row("Profit factor", m.profitFactor.map { String(format: "%.2f", $0) })
            row("Return a year", m.returnPerYearPct.map { String(format: "%.1f%%", $0) })
            row("Sortino", m.sortino.map { String(format: "%.2f", $0) })
            row("Calmar", m.calmar.map { String(format: "%.2f", $0) })
            row("Steady score", m.scoreSteady.map { String(format: "%.2f", $0) })
            row("Profitable months", m.monthsProfitablePct.map { "\(Int($0))%" })
            row("Longest drawdown", m.longestDrawdownDays.map { "\($0) days" })
        }
        Section("Worst cases") {
            row("Worst day", m.worstDay?.pnl.map { Fmt.rupees($0, hidden: hide) + " · " + Fmt.marketDay(m.worstDay?.date) })
            row("Net without the worst day", m.netWithoutWorstDay.map { Fmt.rupees($0, hidden: hide, signed: true) })
            row("Worst week", m.worstWeek.map { "\(Fmt.rupees($0.pnl, hidden: hide)) · \($0.week)" })
            row("Worst trade", m.worstTrade.map { Fmt.rupees($0, hidden: hide) })
            row("Event and gap days", m.netOnEventDays.map { "\(Fmt.rupees($0, hidden: hide, signed: true)) on \(m.eventDaysCount ?? 0) days" })
            row("Net without them", m.netExcludingEventDays.map { Fmt.rupees($0, hidden: hide, signed: true) })
        }
        Section {
            row("Charges × 1.5", m.netWithChargesX15.map { Fmt.rupees($0, hidden: hide, signed: true) })
            if let mc = m.monteCarlo {
                row("Drawdown, 95% of reshuffles", Fmt.rupees(-mc.ddP95, hidden: hide))
                row("Chance the first \(mc.blockTrades) trades lose", "\(mc.pLosingBlock.formatted())%")
            }
            row("Best 5 trades' share of profit", m.top5SharePct.map { "\(Int($0))%" })
        } header: { Text("Robustness") } footer: {
            Text("Reshuffles: the same trades in 1,000 random orders. A strategy that only works in one order is fragile.")
        }
        Section("Margin") {
            row("Peak margin", m.peakMargin.map { Fmt.rupees($0, hidden: hide) })
            row("Average when open", m.avgMarginWhenOpen.map { Fmt.rupees($0, hidden: hide) })
            row("Return on peak margin", m.returnOnPeakMarginPct.map { String(format: "%.1f%%", $0) })
            row("Days over capital", m.daysOverCapitalMargin.map { "\($0)" })
        }
    }

    @ViewBuilder private func audit(_ a: RunAudit, _ m: Metrics) -> some View {
        Section("Data used") {
            row("Days with data", "\(a.dates.withData)")
            row("Dates", "\(Fmt.marketDay(a.dates.first)) – \(Fmt.marketDay(a.dates.last))")
            row("Fills at real prices", a.fills.realPricePct.map { "\($0.formatted())% of \(a.fills.total)" })
            row("Fills at an older price", "\(a.fills.stale)")
            if let n = a.exitsAtOldPrice, n > 0 { row("Exits with no recent trade", "\(n)") }
            row("Orders over 10% of volume", a.fills.over10pctOfVolume.map { "\($0)" })
            row("Index move over the run", m.indexMovePct.map { String(format: "%+.1f%%", $0) })
            ForEach(a.skipped.keys.sorted(), id: \.self) { k in row(humanize(k), "\(a.skipped[k] ?? 0)") }
        }
        Section("Assumptions") {
            row("Fill model", a.fillModel.fillModel == "honest" ? "Honest" : "Classic")
            row("Slippage", a.fillModel.slippagePct.map { "\($0.formatted())% a side" })
            if a.fillModel.fillModel == "honest" {
                row("Oldest trade a fill may use", a.fillModel.fillMaxAgeMin.map { "\($0) min" })
                row("Cost stress", a.fillModel.costStress.map { "× \($0.formatted())" })
            }
            row("Stop order", a.fillModel.slOrder.map { $0 == "limit" ? "SL-limit (may stay unfilled)" : "Assumed to fill" })
            ForEach(a.lotSizesByExpiry.keys.sorted(), id: \.self) { e in
                row("Lot size, expiry \(Fmt.marketDay(e))", (a.lotSizesByExpiry[e] ?? nil).map { "\($0)" })
            }
            ForEach(a.chargesRates.keys.sorted(), id: \.self) { d in
                row("STT on sell, \(Fmt.marketDay(d))", (a.chargesRates[d]?["sttSellPct"] ?? a.chargesRates[d]?["stt_sell_pct"]).map { "\($0.formatted())%" })
            }
            row("Engine", "\(a.engineVersion) · spec \(a.specHash)")
        }
    }

    private func row(_ label: String, _ value: String?) -> some View {
        LabeledContent(label, value: value ?? "—").monospacedDigit()
    }

    @ViewBuilder private var tradesSection: some View {
        Section("Trades (\(totalTrades))") {
            ForEach(trades) { t in
                NavigationLink(value: t) {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(Fmt.storeTime(t.entryTs) + (t.exitTs.isEmpty ? "" : " → " + Fmt.storeTime(t.exitTs)))
                                .font(.subheadline.monospacedDigit())
                            Text((t.fills.first.map { Fmt.contract($0.symbol) } ?? "") + " · " +
                                 humanize(t.exitReason.isEmpty ? "open" : t.exitReason))
                                .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }
                        Spacer()
                        Text(Fmt.rupees(t.net, hidden: hide, signed: true)).monospacedDigit()
                            .foregroundStyle(t.net >= 0 ? Color.tdGood : Color.tdBad)
                    }
                }
            }
            if trades.count < totalTrades {
                Button("Show more") { Task { await moreTrades() } }
            }
        }
    }

    private func statusText(_ r: BacktestRunItem) -> String {
        switch r.status {
        case "queued": return "Waiting to run"
        case "running": return "Running · \(r.progress)%"
        case "failed": return "Failed"
        default: return humanize(r.status)
        }
    }

    // MARK: data

    private var curveSummary: String {
        guard let a = curves.first, let b = curves.last else { return "No data" }
        let dd = curves.map(\.drawdown).min() ?? 0
        return "Equity from \(Fmt.rupees(a.equity, hidden: hide)) to \(Fmt.rupees(b.equity, hidden: hide)), deepest drawdown \(Fmt.rupees(dd, hidden: hide))"
    }

    private func load() async {
        do {
            let d: BacktestDetail = try await model.api.get("/v1/research/backtests/\(runID)")
            detail = d
            if d.run.status == "done" {
                let c: Curves = try await model.api.get("/v1/research/backtests/\(runID)/curves")
                curves = c.days
                let p: TradePage = try await model.api.get("/v1/research/backtests/\(runID)/trades?limit=50")
                trades = p.trades; totalTrades = p.total
            }
            error = nil
            if d.run.status == "queued" || d.run.status == "running" {
                try? await Task.sleep(for: .seconds(5))
                if !Task.isCancelled { await load() }
            }
        } catch { self.error = error.localizedDescription }
    }

    private func moreTrades() async {
        if let p: TradePage = try? await model.api.get("/v1/research/backtests/\(runID)/trades?offset=\(trades.count)&limit=100") {
            trades += p.trades
        }
    }

    private func savePreset(_ sid: Int, accept: Double? = nil) async {
        var body: [String: JSONValue] = ["name": .string("From run \(runID)"), "from_run": .number(Double(runID))]
        if let accept { body["accept_risk"] = .number(accept) }
        do {
            _ = try await model.api.raw("POST", "/v1/strategies/\(sid)/presets", body: JSONValue.object(body))
            error = nil; presetSaved = true
        } catch {
            if let p = PendingAccept.from(error, retry: { await savePreset(sid, accept: $0) }) { pendingAccept = p }
            else { self.error = error.localizedDescription }
        }
    }

    private func rerun() async {
        do {
            let _: BacktestRunItem = try await model.api.post("/v1/research/backtests/\(runID)/rerun")
            dismiss()
        } catch { self.error = error.localizedDescription }
    }

    private func delete() async {
        struct R: Decodable { let ok: Bool }
        do {
            let _: R = try await model.api.send("DELETE", "/v1/research/backtests/\(runID)", body: Optional<String>.none)
            dismiss()
        } catch { self.error = error.localizedDescription }
    }
}

/// Months of days, each a coloured square with a +/− sign (never colour alone).
struct PnlCalendar: View {
    let days: [CurveDay]
    let hide: Bool

    var body: some View {
        let months = Dictionary(grouping: days.filter { $0.pnl != 0 }) { String($0.date.prefix(7)) }
        let maxAbs = max(1, days.map { abs($0.pnl) }.max() ?? 1)
        VStack(alignment: .leading, spacing: 8) {
            ForEach(months.keys.sorted(by: >).prefix(12), id: \.self) { m in
                VStack(alignment: .leading, spacing: 3) {
                    Text(Fmt.month(m)).font(.caption.weight(.semibold))
                    LazyVGrid(columns: Array(repeating: GridItem(.fixed(18), spacing: 3), count: 11), alignment: .leading, spacing: 3) {
                        ForEach(months[m] ?? []) { d in
                            let c = d.pnl >= 0 ? Color.tdGood : Color.tdBad
                            Text(d.pnl >= 0 ? "+" : "−").font(.caption2.weight(.bold)).foregroundStyle(.primary)
                                .frame(width: 18, height: 18)
                                .background(c.opacity(0.25 + 0.45 * abs(d.pnl) / maxAbs), in: .rect(cornerRadius: 3))
                                .accessibilityLabel("\(Fmt.marketDay(d.date)) \(Fmt.rupees(d.pnl, hidden: hide, signed: true))")
                        }
                    }
                }
            }
        }
    }
}

/// One cycle: every buy and sell with time, price, charges and why.
struct TradeDetailView: View {
    @AppStorage("hideAmounts") private var hide = false
    let trade: TradeItem

    var body: some View {
        List {
            Section {
                LabeledContent("Net", value: Fmt.rupees(trade.net, hidden: hide, signed: true))
                LabeledContent("Gross", value: Fmt.rupees(trade.gross, hidden: hide, signed: true))
                LabeledContent("Charges", value: Fmt.rupees(trade.charges, decimals: 2, hidden: hide))
                LabeledContent("Exit", value: humanize(trade.exitReason.isEmpty ? "open" : trade.exitReason))
            }
            .monospacedDigit()
            Section("Orders (IST)") {
                ForEach(trade.fills, id: \.self) { f in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(f.side).font(.caption.weight(.bold)).foregroundStyle(f.side == "BUY" ? Color.tdGood : Color.tdBad)
                            Text(Fmt.contract(f.symbol)).font(.subheadline)
                            Spacer()
                            Text("₹\(String(format: "%.2f", f.price))").monospacedDigit()
                        }
                        HStack {
                            Text(Fmt.storeTime(f.ts, seconds: true))
                            Text("· \(f.qty) qty · \(humanize(f.reason))")
                            if f.stale { Label("older price", systemImage: "clock.badge.exclamationmark").foregroundStyle(Color.tdWarn) }
                        }
                        .font(.caption).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
        .navigationTitle("Trade \(trade.id)")
        .navigationBarTitleDisplayMode(.inline)
    }
}
