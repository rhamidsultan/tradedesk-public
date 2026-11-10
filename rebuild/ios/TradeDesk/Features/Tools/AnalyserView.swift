import Charts
import SwiftUI

/// Trade › Analyser: payoff at expiry and on a chosen day, breakevens, max profit and loss, Greeks, probability of
/// profit, charges and margin for a set of legs, with what-if controls. Prices come from the store unless typed.
/// "Make a strategy draft" turns the legs into a strategy; its stop loss is left for the trader to set.
struct AnalyserView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    @State var underlying: String
    let day: String?
    let expiries: [String]
    let atm: Double?
    @State var legs: [ToolLeg]
    @State private var daysAhead = 0.0
    @State private var ivShift = 0.0
    @State private var spotShift = 0.0
    @State private var result: JSONValue?
    @State private var resultKey = ""            // the inputs `result` belongs to
    @State private var error: String?
    @State private var editing: ToolLeg?
    @State private var adding = false
    @State private var draft: JSONValue?         // {id, ok, problems} of the draft made from these legs
    @State private var draftKey = ""
    @State private var draftError: String?
    @State private var openDraft = false
    @State private var making = false

    init(underlying: String = "NIFTY", day: String? = nil, legs: [ToolLeg] = [], expiries: [String] = [], atm: Double? = nil) {
        _underlying = State(initialValue: underlying)
        self.day = day
        self.expiries = expiries
        self.atm = atm
        _legs = State(initialValue: legs)
    }

    private var legsKey: String {
        "\(underlying)|\(legs.map { "\($0.side)\($0.lots)\($0.strike)\($0.type)\($0.expiry)\($0.price ?? 0)" })"
    }
    private var key: String { "\(legsKey)|\(daysAhead)|\(ivShift)|\(spotShift)" }
    private var fresh: Bool { result != nil && resultKey == key }

    /// Days from the analysis day to the first expiry (the slider never goes past it).
    private var maxDays: Double {
        let f = DateFormatter.ymd
        guard let first = legs.map(\.expiry).min(), let e = f.date(from: first) else { return 10 }
        let start = day.flatMap { f.date(from: $0) } ?? Calendar.current.startOfDay(for: .now)
        let d = Calendar.current.dateComponents([.day], from: start, to: e).day ?? 10
        return Double(max(0, min(30, d)))
    }

    var body: some View {
        List {
            Section {
                Picker("Index", selection: $underlying) {
                    ForEach(OptionChainToolView.underlyings, id: \.self) { Text($0) }
                }
                if legs.isEmpty {
                    Text("No legs yet. Pick them on Trade › Option chain (tap a price), or add one here.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                ForEach(legs) { l in
                    Button { editing = l } label: {
                        HStack {
                            Text(l.title).foregroundStyle(l.side == "SELL" ? Color.tdBad : Color.tdGood)
                            Spacer()
                            Text(l.price.map { "₹" + num($0) } ?? "stored price").font(.caption).foregroundStyle(.secondary)
                        }
                        .frame(minHeight: 44)
                    }
                    .accessibilityLabel(l.title)
                }
                .onDelete { legs.remove(atOffsets: $0) }
                Button("Add a leg", systemImage: "plus") { adding = true }
            } header: { Text("Legs") } footer: {
                Text((day.map { "Prices from \(Fmt.marketDay($0))" } ?? "Prices from the latest stored day") + " unless you type one. Swipe a leg to remove it.")
            }
            if let error {
                Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            }
            if let r = result, !legs.isEmpty {
                Group {
                    riskStrip(r)
                    chartSection(r)
                }
                .opacity(fresh ? 1 : 0.4)
            }
            if !legs.isEmpty { whatIf }
            if let r = result, !legs.isEmpty {
                Group { details(r) }.opacity(fresh ? 1 : 0.4)
            }
            if !legs.isEmpty { draftSection }
        }
        .navigationTitle("Analyser")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $editing) { l in
            LegEditor(leg: l, expiries: expiries) { new in
                if let i = legs.firstIndex(where: { $0.id == l.id }) { if let new { legs[i] = new } else { legs.remove(at: i) } }
                editing = nil
            }
        }
        .sheet(isPresented: $adding) {
            LegEditor(leg: ToolLeg(type: "CE", strike: legs.last?.strike ?? atm ?? 0, side: "SELL",
                                   expiry: legs.last?.expiry ?? expiries.first ?? "", lotSize: legs.last?.lotSize),
                      expiries: expiries, isNew: true) { new in
                if let new { legs.append(new) }
                adding = false
            }
        }
        .navigationDestination(isPresented: $openDraft) {
            if let id = draft?["id"]?.number { StrategyDetailView(strategyID: Int(id)) }
        }
        .onChange(of: maxDays) { _, m in if daysAhead > m { daysAhead = m } }
        .task(id: key) {
            try? await Task.sleep(for: .milliseconds(300))      // settle while a slider moves
            if !Task.isCancelled { await run() }
        }
    }

    private var whatIf: some View {
        Section("What if") {
            slider("Days ahead", value: $daysAhead, range: 0...max(0.5, maxDays), step: 0.5,
                   text: daysAhead == 0 ? "today" : "+\(num(daysAhead, 1)) days")
            slider("Volatility", value: $ivShift, range: -10...10, step: 1, text: ivShift == 0 ? "as now" : "\(ivShift > 0 ? "+" : "")\(Int(ivShift)) points")
            slider("Index move", value: $spotShift, range: -5...5, step: 0.25, text: spotShift == 0 ? "none" : "\(spotShift > 0 ? "+" : "")\(num(spotShift))%")
            if daysAhead != 0 || ivShift != 0 || spotShift != 0 {
                Button("Reset") { daysAhead = 0; ivShift = 0; spotShift = 0 }
            }
        }
    }

    private var draftSection: some View {
        Section {
            if let d = draft, draftKey == legsKey {
                if d["ok"]?.bool != true {
                    Label("Stop loss and max loss a day are not set yet: open the draft › Edit › Risk.", systemImage: "exclamationmark.shield.fill")
                        .foregroundStyle(Color.tdBad).font(.subheadline)
                }
                Button { openDraft = true } label: {
                    Label("Open the draft", systemImage: "arrow.right.circle").frame(maxWidth: .infinity, minHeight: 44)
                }
            } else {
                Button {
                    Task { await makeDraft() }
                } label: {
                    Label(making ? "Making…" : "Make a strategy draft", systemImage: "square.and.pencil")
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .disabled(making || !["NIFTY", "BANKNIFTY", "SENSEX"].contains(underlying))
            }
            if let draftError { Text(draftError).font(.footnote).foregroundStyle(Color.tdBad) }
        } footer: {
            Text("Strikes become steps from ATM and the expiry becomes a rule (nearest or next). Entry at 09:20. You set the stop loss and the max loss per day before it can be saved.")
        }
    }

    private func slider(_ label: String, value: Binding<Double>, range: ClosedRange<Double>, step: Double, text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack { Text(label); Spacer(); Text(text).foregroundStyle(.secondary).monospacedDigit() }
            Slider(value: value, in: range, step: step).accessibilityLabel(label).accessibilityValue(text)
        }
    }

    private func riskStrip(_ r: JSONValue) -> some View {
        Section {
            HStack(alignment: .top) {
                tile("Max loss", r["unlimited_loss"]?.bool == true ? "Unlimited" : money(r["max_loss"]?.number),
                     bad: r["unlimited_loss"]?.bool == true)
                tile("Max profit", r["unlimited_profit"]?.bool == true ? "Unlimited" : money(r["max_profit"]?.number))
                tile("Chance of profit", pct(r["pop"]?.number))
                tile("Margin", r["margin"]?["final"]?.number.map { money($0) } ?? "—")
            }
            if !fresh { Label("Updating…", systemImage: "hourglass").font(.caption).foregroundStyle(.secondary) }
            if r["unlimited_loss"]?.bool == true {
                Text("Unlimited loss: this position needs a stop.").font(.caption).foregroundStyle(Color.tdBad)
            }
        }
    }

    private func tile(_ label: String, _ value: String, bad: Bool = false) -> some View {
        VStack(spacing: 2) {
            Text(value).font(.subheadline.weight(.semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
                .foregroundStyle(bad ? Color.tdBad : .primary)
            Text(label).font(.caption2).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }

    private func chartSection(_ r: JSONValue) -> some View {
        let pts = r["points"]?.array ?? []
        return Section {
            Chart {
                ForEach(Array(pts.enumerated()), id: \.offset) { _, p in
                    if let x = p["x"]?.number, let y = p["expiry"]?.number {
                        LineMark(x: .value("Index", x), y: .value("P&L", y), series: .value("When", "At expiry"))
                            .foregroundStyle(Color.primary)
                    }
                    if let x = p["x"]?.number, let y = p["day"]?.number {
                        LineMark(x: .value("Index", x), y: .value("P&L", y), series: .value("When", "On the day"))
                            .foregroundStyle(Color.accentColor).lineStyle(StrokeStyle(lineWidth: 1.5, dash: [5, 3]))
                    }
                }
                RuleMark(y: .value("Zero", 0)).foregroundStyle(.secondary)
                if let s = r["spot_used"]?.number {
                    RuleMark(x: .value("Index now", s)).foregroundStyle(Color.secondary.opacity(0.6))
                }
                ForEach((r["breakevens"]?.array ?? []).compactMap(\.number), id: \.self) { b in
                    RuleMark(x: .value("Breakeven", b)).foregroundStyle(Color.tdWarn).lineStyle(StrokeStyle(dash: [4, 3]))
                }
            }
            .frame(height: 220)
            .chartYAxis {
                AxisMarks { v in
                    AxisGridLine()
                    AxisValueLabel { if !hide, let d = v.as(Double.self) { Text(Fmt.rupeesShort(d)) } }
                }
            }
            .accessibilityLabel("Payoff chart. Solid line at expiry, dashed line \(daysAhead == 0 ? "today" : "in \(num(daysAhead, 1)) days").")
            Text("Solid: at expiry · Dashed: \(daysAhead == 0 ? "today" : "+\(num(daysAhead, 1)) days") · Grey: index now · Orange: breakevens")
                .font(.caption2).foregroundStyle(.secondary)
        } header: {
            Text("Payoff · \(r["underlying"]?.string ?? "") \(num(r["spot"]?.number)) on \(Fmt.marketDay(r["day"]?.string)), \(r["at"]?.string ?? "") IST")
        }
    }

    @ViewBuilder private func details(_ r: JSONValue) -> some View {
        Section("Risk and reward") {
            LabeledContent("P&L at these settings", value: money(r["pnl_now"]?.number, signed: true))
            LabeledContent("Breakevens", value: (r["breakevens"]?.array ?? []).compactMap(\.number).map { num($0, 0) }.joined(separator: " · ").ifEmpty("—"))
            LabeledContent((r["net_premium"]?.number ?? 0) >= 0 ? "Premium received" : "Premium paid", value: money(abs(r["net_premium"]?.number ?? 0)))
            LabeledContent("Charges, in and out", value: money(r["charges_round_trip"]?.number))
            if let m = r["margin"]?["final"]?.number {
                LabeledContent("Margin (Zerodha, \(r["margin"]?["at"]?.string ?? "") IST)", value: money(m))
            } else if let why = r["margin"]?["unknown"]?.string {
                LabeledContent("Margin") { Text(why).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.trailing) }
            }
        }
        Section {
            let g = r["greeks"]
            LabeledContent("Delta (₹ per index point)", value: money(g?["delta"]?.number, signed: true))
            LabeledContent("Gamma (delta change per point)", value: num(g?["gamma"]?.number, 4))
            LabeledContent("Theta (₹ a day)", value: money(g?["theta"]?.number, signed: true))
            LabeledContent("Vega (₹ a vol point)", value: money(g?["vega"]?.number, signed: true))
        } header: { Text("Greeks for the position") } footer: {
            Text("Black-Scholes with each leg's own implied volatility (" + (r["legs"]?.array ?? []).map { pct($0["iv"]?.number) }.joined(separator: ", ")
                 + "). Chance of profit assumes a lognormal index at the average IV; it is a guide, not a forecast.")
        }
    }

    private func money(_ v: Double?, signed: Bool = false) -> String {
        guard let v else { return "—" }
        return Fmt.rupees(v, hidden: hide, signed: signed)
    }

    private func run() async {
        let k = key
        guard !legs.isEmpty, legs.allSatisfy({ $0.strike > 0 && !$0.expiry.isEmpty }) else { result = nil; return }
        var body: [String: JSONValue] = ["underlying": .string(underlying), "legs": .array(legs.map(\.json)),
                                         "days_ahead": .number(daysAhead), "iv_shift": .number(ivShift),
                                         "spot_shift_pct": .number(spotShift)]
        if let day { body["day"] = .string(day) }
        do {
            let r = try await model.api.raw("POST", "/v1/tools/analyse", body: JSONValue.object(body))
            guard k == key else { return }
            result = r
            resultKey = k
            error = nil
        } catch is CancellationError {
        } catch {
            guard k == key else { return }
            result = nil                 // never show figures for other legs next to an error
            self.error = error.localizedDescription
        }
    }

    private func makeDraft() async {
        making = true
        defer { making = false }
        var body: [String: JSONValue] = ["underlying": .string(underlying), "legs": .array(legs.map(\.json))]
        if let day { body["day"] = .string(day) }
        do {
            draft = try await model.api.raw("POST", "/v1/tools/to-strategy", body: JSONValue.object(body))
            draftKey = legsKey
            draftError = nil
            openDraft = true
        } catch {
            draftError = error.localizedDescription
        }
    }
}

extension DateFormatter {
    /// yyyy-MM-dd as a calendar day: Gregorian, POSIX, the phone's own time zone both ways.
    static let ymd: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}

private extension String {
    func ifEmpty(_ s: String) -> String { isEmpty ? s : self }
}

/// Edit or add one leg: side, call or put, strike, lots, expiry and an optional price.
struct LegEditor: View {
    @State var leg: ToolLeg
    let original: ToolLeg
    let expiries: [String]
    var isNew = false
    var done: (ToolLeg?) -> Void
    @State private var priceText = ""
    @State private var expiryDate = Date()

    init(leg: ToolLeg, expiries: [String] = [], isNew: Bool = false, done: @escaping (ToolLeg?) -> Void) {
        var l = leg
        if l.expiry.isEmpty, let e = expiries.first { l.expiry = e }
        _leg = State(initialValue: l)
        original = leg
        self.expiries = expiries
        self.isNew = isNew
        self.done = done
        _priceText = State(initialValue: leg.price.map { String($0) } ?? "")
        _expiryDate = State(initialValue: DateFormatter.ymd.date(from: l.expiry) ?? .now)
    }

    var body: some View {
        NavigationStack {
            Form {
                Picker("Side", selection: $leg.side) { Text("Buy").tag("BUY"); Text("Sell").tag("SELL") }.pickerStyle(.segmented)
                Picker("Type", selection: $leg.type) { Text("Call (CE)").tag("CE"); Text("Put (PE)").tag("PE") }.pickerStyle(.segmented)
                LabeledContent("Strike") {
                    TextField("25000", value: $leg.strike, format: .number.grouping(.never)).keyboardType(.numberPad)
                        .multilineTextAlignment(.trailing)
                }
                Stepper(leg.lotsText, value: $leg.lots, in: 1...50)
                if expiries.isEmpty {
                    DatePicker("Expiry", selection: $expiryDate, displayedComponents: .date)
                } else {
                    Picker("Expiry", selection: $leg.expiry) {
                        ForEach(expiries, id: \.self) { Text(Fmt.marketDay($0)).tag($0) }
                    }
                }
                LabeledContent("Price (optional)") {
                    TextField("stored price", text: $priceText).keyboardType(.decimalPad).multilineTextAlignment(.trailing)
                }
                if !isNew {
                    Button("Remove this leg", role: .destructive) { done(nil) }
                }
            }
            .navigationTitle(isNew ? "Add a leg" : "Edit leg")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { done(isNew ? nil : original) } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        if expiries.isEmpty { leg.expiry = DateFormatter.ymd.string(from: expiryDate) }
                        leg.price = Double(priceText.replacingOccurrences(of: ",", with: ""))
                        done(leg)
                    }
                    .disabled(leg.strike <= 0)
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}
