import Charts
import SwiftUI

/// More › Portfolio › a stock or Gold: money invested against value, P&L with the day's high–low band, money added
/// each month, trades and corporate actions, and a check that the history adds up to Zerodha's holding.
struct StockHistoryView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    @State var symbol: String
    let gold: Bool
    @State private var data: JSONValue?
    @State private var error: String?
    @State private var range = "1Y"            // 3M | 1Y | All

    private struct Choice: Identifiable { let id: String; let held: Bool }

    var body: some View {
        List {
            if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            if let d = data {
                let choices = (d["choices"]?.array ?? []).compactMap { c -> Choice? in
                    guard let s = c["symbol"]?.string else { return nil }
                    return Choice(id: s, held: c["held"]?.bool == true)
                }
                if choices.isEmpty {
                    Text(gold ? "No gold holdings or gold trades yet." : "No stocks yet. Import a tradebook on the portfolio page.")
                        .foregroundStyle(.secondary)
                } else {
                    Picker(gold ? "Gold fund" : "Stock", selection: $symbol) {
                        ForEach(choices) { c in Text(c.held ? c.id : "\(c.id) (sold)").tag(c.id) }
                    }
                }
                if let s = d["summary"]?.object, !s.isEmpty { summary(d) }
                recon(d)
                charts(d)
                monthly(d)
                trades(d)
                actions(d)
                Section { Text(d["price_source"]?.string ?? "").font(.footnote).foregroundStyle(.secondary) }
            } else if error == nil {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle(symbol.isEmpty ? (gold ? "Gold" : "Stock history") : symbol)
        .navigationBarTitleDisplayMode(.inline)
        .task(id: symbol) { await load() }
        .refreshable { await load() }
    }

    private func money(_ v: Double?, signed: Bool = false) -> String {
        guard let v else { return "—" }
        return Fmt.rupees(v, hidden: hide, signed: signed)
    }

    private func summary(_ d: JSONValue) -> some View {
        let s = d["summary"]
        let pnl = s?["pnl"]?.number ?? 0
        return Section {
            LabeledContent("Value", value: money(s?["value"]?.number))
            LabeledContent("Invested", value: money(s?["invested"]?.number))
            LabeledContent("Profit or loss") {
                Text("\(money(pnl, signed: true)) · \(String(format: "%+.1f%%", s?["pnl_pct"]?.number ?? 0))")
                    .foregroundStyle(pnl > 0 ? Color.tdGood : pnl < 0 ? Color.tdBad : .primary).monospacedDigit()
            }
            if !hide, let q = s?["qty"]?.number { LabeledContent("Units", value: num(q, 0)) }
            LabeledContent("Average cost", value: num(s?["avg_cost"]?.number))
            LabeledContent("Booked profit", value: s?["realized"]?.number.map { money($0, signed: true) } ?? "Unknown (units don't add up)")
            if let f = s?["first_day"]?.string { LabeledContent("Held since", value: Fmt.marketDay(f)) }
        }
    }

    @ViewBuilder private func recon(_ d: JSONValue) -> some View {
        let hidden = d["amounts_hidden"]?.bool == true
        if d["recon"]?["ok"]?.bool == false {
            Section {
                Label(hidden ? "The trade history does not add up to Zerodha's holding yet."
                      : "Trades and corporate actions do not add up to your Zerodha holding (\(num(d["recon"]?["holding_qty"]?.number, 0)) held, \(num(d["recon"]?["trade_qty"]?.number, 0)) from history). An action or an off-market transfer is missing, or a tradebook has not been imported.",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(.footnote).foregroundStyle(Color.tdWarn)
            }
        } else if d["recon"]?["ok"]?.bool == true, d["trade_count"]?.number ?? 1 > 0 {
            Section {
                Label(hidden || d["recon"]?["holding_qty"]?.number == nil ? "Adds up to Zerodha's holding"
                      : "Adds up to Zerodha's holding: \(num(d["recon"]?["holding_qty"]?.number, 0)) units",
                      systemImage: "checkmark.seal.fill")
                    .font(.footnote).foregroundStyle(Color.tdGood)
            }
        }
    }

    private func cut(_ rows: [JSONValue]) -> [JSONValue] {
        let days = range == "3M" ? 92 : range == "1Y" ? 366 : 100_000
        let start = Calendar.current.date(byAdding: .day, value: -days, to: .now) ?? .distantPast
        let f = DateFormatter.ymd
        return rows.filter { (f.date(from: $0["day"]?.string ?? "") ?? .now) >= start }
    }

    @ViewBuilder private func charts(_ d: JSONValue) -> some View {
        let rows = cut(d["series"]?.array ?? [])
        if d["amounts_hidden"]?.bool == true, !rows.isEmpty {
            Section {
                Picker("Range", selection: $range) { Text("3M").tag("3M"); Text("1Y").tag("1Y"); Text("All").tag("All") }
                    .pickerStyle(.segmented)
                Chart(Array(rows.enumerated()), id: \.offset) { _, r in
                    LineMark(x: .value("Day", DateFormatter.ymd.date(from: r["day"]?.string ?? "") ?? .now),
                             y: .value("Profit or loss %", r["pnl_pct"]?.number ?? 0))
                }
                .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if let x = v.as(Double.self) { Text("\(Int(x))%") } } } }
                .frame(height: 180)
                .accessibilityLabel("Profit or loss in percent over time")
            } header: { Text("Profit or loss %, over time") }
        } else if !rows.isEmpty {
            Section {
                Picker("Range", selection: $range) { Text("3M").tag("3M"); Text("1Y").tag("1Y"); Text("All").tag("All") }
                    .pickerStyle(.segmented)
                Chart(Array(rows.enumerated()), id: \.offset) { _, r in
                    LineMark(x: .value("Day", DateFormatter.ymd.date(from: r["day"]?.string ?? "") ?? .now),
                             y: .value("₹", r["invested"]?.number ?? 0))
                        .foregroundStyle(by: .value("Line", "Invested"))
                        .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [5, 3]))
                    LineMark(x: .value("Day", DateFormatter.ymd.date(from: r["day"]?.string ?? "") ?? .now),
                             y: .value("₹", r["value"]?.number ?? 0))
                        .foregroundStyle(by: .value("Line", "Value"))
                }
                .chartForegroundStyleScale(["Invested": Color.secondary, "Value": Color.accentColor])
                .chartLegend(position: .top)
                .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if !hide, let x = v.as(Double.self) { Text(Fmt.rupeesShort(x)) } } } }
                .frame(height: 180)
                .accessibilityLabel("Invested amount and value over time")
                Text("Dashed: money invested · Solid: value at the close").font(.caption2).foregroundStyle(.secondary)
                Chart(Array(rows.enumerated()), id: \.offset) { _, r in
                    let day = DateFormatter.ymd.date(from: r["day"]?.string ?? "") ?? .now
                    AreaMark(x: .value("Day", day), yStart: .value("Low", r["pnl_low"]?.number ?? 0),
                             yEnd: .value("High", r["pnl_high"]?.number ?? 0))
                        .foregroundStyle(Color.accentColor.opacity(0.2))
                    LineMark(x: .value("Day", day), y: .value("P&L", r["pnl"]?.number ?? 0))
                }
                .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if !hide, let x = v.as(Double.self) { Text(Fmt.rupeesShort(x)) } } } }
                .frame(height: 160)
                .accessibilityLabel("Profit or loss over time, with each day's high and low")
                Text("Profit or loss at the close; band: at the day's high and low").font(.caption2).foregroundStyle(.secondary)
            } header: { Text("Over time") }
        }
    }

    @ViewBuilder private func monthly(_ d: JSONValue) -> some View {
        let m = Array((d["monthly"]?.array ?? []).suffix(24))
        if !m.isEmpty {
            Section("Money added each month") {
                Chart(Array(m.enumerated()), id: \.offset) { _, r in
                    BarMark(x: .value("Month", r["month"]?.string ?? ""),
                            y: .value("Net added", (r["bought"]?.number ?? 0) - (r["sold"]?.number ?? 0)))
                        .foregroundStyle(((r["bought"]?.number ?? 0) - (r["sold"]?.number ?? 0)) >= 0 ? Color.accentColor : Color.tdWarn)
                }
                .chartXAxis { AxisMarks(values: .automatic(desiredCount: 4)) }
                .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if !hide, let x = v.as(Double.self) { Text(Fmt.rupeesShort(x)) } } } }
                .frame(height: 140)
                .accessibilityLabel("Money added each month: bought minus sold")
            }
        }
    }

    @ViewBuilder private func trades(_ d: JSONValue) -> some View {
        let t = d["recent"]?.array ?? []
        if !t.isEmpty {
            Section {
                ForEach(Array(t.prefix(30).enumerated()), id: \.offset) { _, x in
                    HStack {
                        Text(Fmt.marketDay(x["day"]?.string)).font(.caption).frame(width: 90, alignment: .leading)
                        Text(x["side"]?.string == "buy" ? "Buy" : "Sell")
                            .foregroundStyle(x["side"]?.string == "buy" ? Color.tdGood : Color.tdBad)
                        Spacer()
                        Text(hide ? "@ \(num(x["price"]?.number))" : "\(num(x["qty"]?.number, 0)) @ \(num(x["price"]?.number))")
                            .monospacedDigit().font(.subheadline)
                    }
                }
            } header: {
                Text("Trades · \(Int(d["trade_count"]?.number ?? 0)) from \(Fmt.marketDay(d["first_trade"]?.string)) to \(Fmt.marketDay(d["last_trade"]?.string))")
            }
        }
    }

    @ViewBuilder private func actions(_ d: JSONValue) -> some View {
        let a = d["actions"]?.array ?? []
        if !a.isEmpty {
            Section("Corporate actions") {
                ForEach(Array(a.enumerated()), id: \.offset) { _, x in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(Fmt.marketDay(x["day"]?.string)) · \(x["text"]?.string ?? "")").font(.subheadline)
                        if let b = x["basis"]?.string, !b.isEmpty { Text(b).font(.caption).foregroundStyle(.secondary) }
                    }
                }
            }
        }
    }

    private func load() async {
        var q = "/v1/portfolio/stock?symbol=\(symbol.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")"
        if gold { q += "&gold=true" }
        do {
            let d = try await model.api.raw("GET", q)
            data = d
            if let s = d["symbol"]?.string, s != symbol { symbol = s }
            error = nil
        } catch { self.error = "Couldn't load the history: \(error.localizedDescription)" }
    }
}

/// More › Portfolio › US stocks (IBKR): holdings, cash, account value, $ / ₹ switch, today and by-day charts.
/// The owner can start, stop or restart IB Gateway (frees about 1 GB of memory on the server when stopped).
struct USStocksView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    @AppStorage("usInRupees") private var inRupees = false
    @State private var data: JSONValue?
    @State private var error: String?
    @State private var chart = "today"
    @State private var gateway: String?
    @State private var note: String?

    var body: some View {
        List {
            if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            if let note { Label(note, systemImage: "info.circle").font(.footnote) }
            if let d = data {
                if d["ok"]?.bool != true {
                    Section { Text(d["error"]?.string ?? "No US data.").foregroundStyle(.secondary) }
                } else {
                    header(d)
                    summary(d)
                    chartSection(d)
                    positions(d)
                }
                if model.user?.role == "owner", d["installed"]?.bool == true { control(d) }
            } else if error == nil {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("US stocks")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Picker("Currency", selection: $inRupees) { Text("Dollars ($)").tag(false); Text("Rupees (₹)").tag(true) }
                } label: {
                    Text(inRupees ? "₹" : "$").font(.headline).frame(minWidth: 44, minHeight: 44)
                }
                .accessibilityLabel("Show amounts in \(inRupees ? "rupees" : "dollars"). Change")
            }
        }
        .refreshable { await load() }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(for: .seconds(60))
            }
        }
        .confirmationDialog("\(gateway?.capitalized ?? "") IB Gateway?", isPresented: Binding(get: { gateway != nil }, set: { if !$0 { gateway = nil } }),
                            titleVisibility: .visible) {
            Button(gateway?.capitalized ?? "", role: gateway == "stop" ? .destructive : nil) {
                if let g = gateway { Task { await sendControl(g) } }
            }
        } message: {
            Text(gateway == "stop" ? "US holdings stop updating until you start it again. Nothing is deleted."
                 : "IBKR may ask you to approve the login in IBKR Mobile.")
        }
    }

    private var fx: Double { data?["usd_inr"]?.number ?? 0 }

    private func money(_ usd: Double?, signed: Bool = false) -> String {
        guard let usd else { return "—" }
        return inRupees && fx > 0 ? Fmt.rupees(usd * fx, hidden: hide, signed: signed) : Fmt.dollars(usd, hidden: hide, signed: signed)
    }

    private func header(_ d: JSONValue) -> some View {
        HStack {
            if d["stale"]?.bool == true {
                StatusPill(text: "Stale · \(num(d["age_min"]?.number, 0)) min old", systemImage: "clock.badge.exclamationmark", color: .tdWarn)
            } else if d["in_window"]?.bool == true {
                StatusPill(text: "Live", systemImage: "dot.radiowaves.left.and.right", color: .tdGood)
            } else {
                StatusPill(text: "US market closed", systemImage: "moon", color: .secondary)
            }
            Spacer()
            Text("Updated \(d["updated_ist"]?.string ?? "—")").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func summary(_ d: JSONValue) -> some View {
        let s = d["summary"]
        let value = d["value"]?.number ?? 0, invested = d["invested"]?.number ?? 0
        return Section {
            LabeledContent("Account value", value: money(s?["net_liquidation"]?.number ?? s?["net_liq"]?.number))
            let today = s?["daily_pnl"]?.number ?? 0
            LabeledContent("Today") {
                Text((today > 0 ? "▲ " : today < 0 ? "▼ " : "") + money(today, signed: true)).monospacedDigit()
                    .foregroundStyle(today > 0 ? Color.tdGood : today < 0 ? Color.tdBad : .primary)
            }
            .accessibilityLabel("US stocks today: \(today >= 0 ? "profit" : "loss") \(money(abs(today)))")
            LabeledContent("Holdings value", value: money(value))
            LabeledContent("Invested", value: money(invested))
            LabeledContent("Profit or loss", value: money(value - invested, signed: true))
            if let c = s?["cash"]?.number { LabeledContent("Cash", value: money(c)) }
            LabeledContent("USD/INR", value: num(fx))
        }
    }

    @ViewBuilder private func chartSection(_ d: JSONValue) -> some View {
        Section {
            Picker("Chart", selection: $chart) { Text("Today").tag("today"); Text("By day").tag("days") }.pickerStyle(.segmented)
            if chart == "today" {
                let rows = d["intraday"]?.array ?? []
                Chart(Array(rows.enumerated()), id: \.offset) { _, r in
                    LineMark(x: .value("Time (IST)", r["t"]?.string ?? ""), y: .value("Day P&L", r["daily_pnl"]?.number ?? 0))
                }
                .chartXAxis { AxisMarks(values: .automatic(desiredCount: 4)) }
                .chartYAxis(hide ? .hidden : .automatic)
                .frame(height: 180)
                .accessibilityLabel("US day P&L through the session")
            } else {
                let rows = Array((d["days"]?.array ?? []).suffix(120))
                Chart(Array(rows.enumerated()), id: \.offset) { _, r in
                    LineMark(x: .value("Day", r["day"]?.string ?? ""), y: .value("Account value", r["net_liq"]?.number ?? 0))
                }
                .chartXAxis(.hidden)
                .chartYAxis(hide ? .hidden : .automatic)
                .frame(height: 180)
                .accessibilityLabel("US account value by day")
            }
        } footer: { Text("Charts are in dollars.") }
    }

    private func positions(_ d: JSONValue) -> some View {
        Section("Holdings") {
            ForEach(Array((d["positions"]?.array ?? []).enumerated()), id: \.offset) { _, p in
                let pnl = p["unrealized_pnl"]?.number ?? ((p["market_value"]?.number ?? 0) - (p["qty"]?.number ?? 0) * (p["avg_cost"]?.number ?? 0))
                if d["amounts_hidden"]?.bool == true {
                    let pp = p["pnl_pct"]?.number ?? 0
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(p["symbol"]?.string ?? "").font(.subheadline.weight(.semibold))
                            Text("\(Int(p["weight_pct"]?.number ?? 0))% of US holdings").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text((pp > 0 ? "▲ " : pp < 0 ? "▼ " : "") + String(format: "%+.1f%%", pp)).monospacedDigit()
                            .foregroundStyle(pp > 0 ? Color.tdGood : pp < 0 ? Color.tdBad : .primary)
                    }
                    .accessibilityElement(children: .combine)
                } else {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p["symbol"]?.string ?? "").font(.subheadline.weight(.semibold))
                        Text(hide || p["qty"]?.number == nil ? "Avg \(num(p["avg_cost"]?.number))"
                             : "\(num(p["qty"]?.number, 0)) × avg \(num(p["avg_cost"]?.number))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(money(p["market_value"]?.number)).monospacedDigit()
                        Text(money(pnl, signed: true)).font(.caption).monospacedDigit()
                            .foregroundStyle(pnl > 0 ? Color.tdGood : pnl < 0 ? Color.tdBad : .secondary)
                    }
                }
                .accessibilityElement(children: .combine)
                }
            }
        }
    }

    private func control(_ d: JSONValue) -> some View {
        Section {
            if let c = d["control"], c != .null {
                Text("Last request: \(c["action"]?.string ?? "") · \(c["status"]?.string ?? "") · \(c["message"]?.string ?? "")")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Start") { gateway = "start" }
                Button("Restart") { gateway = "restart" }
                Button("Stop", role: .destructive) { gateway = "stop" }
            }
            .buttonStyle(.bordered).controlSize(.large)
        } header: { Text("IB Gateway on the server") }
    }

    private func load() async {
        do { data = try await model.api.raw("GET", "/v1/us"); error = nil }
        catch { self.error = "Couldn't load US stocks: \(error.localizedDescription)" }
    }

    private func sendControl(_ action: String) async {
        do {
            _ = try await model.api.raw("POST", "/v1/us/gateway", body: JSONValue.object(["action": .string(action)]))
            note = "Asked the server to \(action) IB Gateway. It takes a minute or two."
            gateway = nil
            await load()
        } catch { self.error = error.localizedDescription }
    }
}
