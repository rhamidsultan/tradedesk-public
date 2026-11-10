import SwiftUI

// The builder's steps. Each edits the spec through SpecBinding; the server checks everything on each change.

struct InstrumentStep: View {
    let bind: SpecBinding
    var body: some View {
        Form {
            Picker("Index", selection: bind.text("instrument.underlying", "NIFTY")) {
                ForEach(["NIFTY", "BANKNIFTY", "SENSEX"], id: \.self) { Text($0) }
            }
            Picker("Bar length", selection: bind.text("instrument.timeframe", "5m")) {
                ForEach(["1m", "3m", "5m", "15m"], id: \.self) { Text($0.replacingOccurrences(of: "m", with: " min")) }
            }
            Section { Text("Options on the index. Signals use closed bars of this length; stops are checked every minute.").font(.footnote) }
        }
        .navigationTitle("Instrument")
    }
}

/// Series the engine knows (see py/spec): price, moving averages, RSI, ATR, Supertrend, opening range, day levels.
let SERIES = ["close", "open", "high", "low", "ema(20)", "ema(50)", "sma(20)", "rsi(14)", "atr(14)",
              "supertrend(10,3)", "supertrend_dir(10,3)", "opening_range_high(15)", "opening_range_low(15)",
              "day_high", "day_low", "prev_day_high", "prev_day_low", "prev_day_close"]
let OPS = [">", "<", ">=", "<=", "crosses_above", "crosses_below"]

struct EntryStep: View {
    let bind: SpecBinding
    var rules: [JSONValue] { bind.get("entry.any_of")?.array ?? [] }

    var body: some View {
        Form {
            ForEach(rules.indices, id: \.self) { i in
                Section {
                    TextField("Name (legs open on it)", text: bind.text("entry.any_of.\(i).action"))
                    Toggle("At a set time", isOn: Binding(
                        get: { bind.get("entry.any_of.\(i).at_time")?.string != nil },
                        set: { on in bind.set("entry.any_of.\(i).at_time", on ? .string("09:20") : nil) }))
                    if bind.get("entry.any_of.\(i).at_time")?.string != nil {
                        DatePicker("Time (IST)", selection: bind.time("entry.any_of.\(i).at_time", "09:20"), displayedComponents: .hourAndMinute)
                            .environment(\.timeZone, Fmt.ist)
                    }
                    let conds = bind.get("entry.any_of.\(i).all_of")?.array ?? []
                    ForEach(conds.indices, id: \.self) { j in
                        ConditionRow(bind: bind, path: "entry.any_of.\(i).all_of.\(j)")
                    }
                    Button("Add a condition", systemImage: "plus") {
                        var list = conds
                        list.append(.object(["left": .string("close"), "op": .string("crosses_above"), "right": .string("ema(20)")]))
                        bind.set("entry.any_of.\(i).all_of", .array(list))
                    }
                    Button("Remove this rule", role: .destructive) {
                        var list = rules; list.remove(at: i); bind.set("entry.any_of", .array(list))
                    }
                } header: { Text("Rule \(i + 1)") } footer: {
                    Text("All conditions must be true on a closed bar. Entries fill at the next minute's trade.")
                }
            }
            Button("Add an entry rule", systemImage: "plus.circle") {
                var list = rules
                list.append(.object(["action": .string(rules.isEmpty ? "enter" : "enter\(rules.count + 1)"),
                                     "at_time": .string("09:20"), "all_of": .array([])]))
                bind.set("entry.any_of", .array(list))
            }
        }
        .navigationTitle("Entry")
    }
}

struct ConditionRow: View {
    let bind: SpecBinding
    let path: String
    var body: some View {
        VStack(alignment: .leading) {
            Picker("When", selection: bind.text("\(path).left", "close")) { ForEach(SERIES, id: \.self) { Text($0) } }
            Picker("is", selection: bind.text("\(path).op", ">")) {
                ForEach(OPS, id: \.self) { Text($0.replacingOccurrences(of: "_", with: " ")) }
            }
            let isNumber = bind.get("\(path).right")?.number != nil
            Picker("than", selection: Binding(get: { isNumber ? "a number" : "a series" }, set: { v in
                bind.set("\(path).right", v == "a number" ? .number(0) : .string("ema(20)"))
            })) { Text("a series").tag("a series"); Text("a number").tag("a number") }
            if isNumber {
                NumberField(label: "Number", value: bind.number("\(path).right"))
            } else {
                Picker("Series", selection: bind.text("\(path).right", "ema(20)")) { ForEach(SERIES, id: \.self) { Text($0) } }
            }
        }
    }
}

struct LegsStep: View {
    let bind: SpecBinding
    var legs: [JSONValue] { bind.get("legs")?.array ?? [] }
    var actions: [String] { (bind.get("entry.any_of")?.array ?? []).compactMap { $0["action"]?.string } }

    var body: some View {
        Form {
            ForEach(legs.indices, id: \.self) { i in
                let p = "legs.\(i)"
                Section("Leg \(i + 1)") {
                    Picker("Opens on", selection: bind.text("\(p).when")) { ForEach(actions, id: \.self) { Text($0) } }
                    Picker("Buy or sell", selection: bind.text("\(p).side", "SELL")) { Text("Buy").tag("BUY"); Text("Sell").tag("SELL") }
                        .pickerStyle(.segmented)
                    Picker("Call or put", selection: bind.text("\(p).type", "CE")) { Text("Call (CE)").tag("CE"); Text("Put (PE)").tag("PE") }
                        .pickerStyle(.segmented)
                    Picker("Expiry", selection: bind.text("\(p).expiry", "nearest_weekly")) {
                        Text("Nearest weekly").tag("nearest_weekly"); Text("Next weekly").tag("next_weekly")
                        Text("Nearest monthly").tag("nearest_monthly")
                    }
                    Picker("Strike by", selection: bind.text("\(p).strike.by", "atm_offset")) {
                        Text("Strikes from ATM").tag("atm_offset"); Text("Premium (₹)").tag("premium")
                    }
                    if bind.get("\(p).strike.by")?.string == "premium" {
                        NumberField(label: "Premium near", value: bind.number("\(p).strike.value"), unit: "₹")
                        Text("The strike whose premium is closest to this at entry.").font(.caption).foregroundStyle(.secondary)
                    } else {
                        StrikePicker(bind: bind, path: p)
                    }
                    Toggle("Lots from risk", isOn: Binding(get: { bind.get("\(p).lots")?.string == "from_risk" },
                                                           set: { bind.set("\(p).lots", $0 ? .string("from_risk") : .number(1)) }))
                    if bind.get("\(p).lots")?.string != "from_risk" {
                        Stepper("Lots: \(bind.int("\(p).lots", 1).wrappedValue)", value: bind.int("\(p).lots", 1), in: 1...50)
                            .frame(minHeight: 44)
                    } else {
                        Text("Lots = risk per trade ÷ loss a lot at the stop, capped by max lots.").font(.caption).foregroundStyle(.secondary)
                    }
                    Button("Remove this leg", role: .destructive) {
                        var list = legs; list.remove(at: i); bind.set("legs", .array(list))
                    }
                }
            }
            Button("Add a leg", systemImage: "plus.circle") {
                var list = legs
                list.append(.object(["when": .string(actions.first ?? "enter"), "side": .string("SELL"), "type": .string("CE"),
                                     "expiry": .string("nearest_weekly"),
                                     "strike": .object(["by": .string("premium"), "value": .number(40)]), "lots": .number(1)]))
                bind.set("legs", .array(list))
            }
            .disabled(actions.isEmpty)
            if actions.isEmpty { Text("Add an entry rule first: legs open on an entry rule's name.").font(.footnote) }
        }
        .navigationTitle("Legs")
    }
}

struct RiskStep: View {
    let bind: SpecBinding
    var body: some View {
        Form {
            Section {
                Picker("Measured on", selection: bind.text("risk.stop_loss.on", "premium")) {
                    Text("Option premium (at Zerodha)").tag("premium"); Text("Index (app-held)").tag("underlying")
                }
                Picker("As", selection: bind.text("risk.stop_loss.type", "multiple")) {
                    Text("Multiple of premium").tag("multiple"); Text("Percent").tag("percent"); Text("Points").tag("points")
                }
                NumberField(label: "Value", value: bind.number("risk.stop_loss.value"), unit: unit(bind.get("risk.stop_loss.type")?.string))
                Text(stopSentence).font(.callout)
            } header: { Text("Stop loss (required)") } footer: {
                Text(bind.get("risk.stop_loss.on")?.string == "underlying"
                     ? "An index stop is held by the app: it cannot sit at Zerodha, so it stops working if the app or server is down. Selling options needs a premium stop."
                     : "A premium stop is placed at Zerodha as an SL order, so it works even if the app or server is down.")
            }
            Section("Limits (required)") {
                NumberField(label: "Max loss a day", value: bind.number("risk.max_loss_per_day"), unit: "₹")
                Stepper("Max lots: \(bind.int("risk.max_lots", 10).wrappedValue)", value: bind.int("risk.max_lots", 10), in: 1...100)
                Stepper("Max trades a day: \(bind.int("risk.max_trades_per_day", 2).wrappedValue)",
                        value: bind.int("risk.max_trades_per_day", 2), in: 1...20)
                NumberField(label: "Risk per trade (lots from risk)", value: bind.positive("risk.risk_per_trade"), unit: "₹")
            }
            Section("Target") {
                Toggle("Use a target", isOn: Binding(get: { bind.get("risk.target") != nil }, set: { on in
                    bind.set("risk.target", on ? .object(["on": bind.get("risk.stop_loss.on") ?? .string("premium"),
                                                          "type": .string("rr"), "value": .number(2)]) : nil)
                }))
                if bind.get("risk.target") != nil {
                    Picker("As", selection: bind.text("risk.target.type", "rr")) {
                        Text("Risk : reward").tag("rr"); Text("Percent").tag("percent"); Text("Points").tag("points"); Text("Multiple").tag("multiple")
                    }
                    NumberField(label: bind.get("risk.target.type")?.string == "rr" ? "Reward for each 1 of risk" : "Value",
                                value: bind.number("risk.target.value"), unit: unit(bind.get("risk.target.type")?.string))
                }
            }
            Section {
                Toggle("Trailing stop", isOn: Binding(get: { bind.get("risk.trailing") != nil }, set: { on in
                    bind.set("risk.trailing", on ? .object(["type": .string("points"), "value": .number(10),
                                                            "activate_after": .number(10), "step": .number(1)]) : nil)
                }))
                if bind.get("risk.trailing") != nil {
                    NumberField(label: "Trail by", value: bind.number("risk.trailing.value"), unit: "pts")
                    NumberField(label: "Start after a gain of", value: bind.number("risk.trailing.activate_after"), unit: "pts")
                    NumberField(label: "Move in steps of", value: bind.number("risk.trailing.step"), unit: "pts")
                }
                NumberField(label: "Close the whole trade at a loss of", value: bind.positive("risk.position_stop"), unit: "₹")
                NumberField(label: "…or a profit of", value: bind.positive("risk.position_target"), unit: "₹")
                Stepper("Re-entries after a stop: \(bind.int("risk.reentry_after_sl").wrappedValue)", value: bind.int("risk.reentry_after_sl"), in: 0...5)
                NumberField(label: "Re-enter only this far past the stop", value: bind.number("risk.reentry_gap"), unit: "pts")
                Toggle("Close a lone leg at 15:25", isOn: bind.bool("risk.close_lone_leg_eod"))
            } header: { Text("More") } footer: { Text("Leave a ₹ field at 0 to not use it.") }
        }
        .navigationTitle("Risk")
        .onAppear { writeDefaults() }
    }

    private func unit(_ type: String?) -> String {
        switch type {
        case "multiple": return "×"
        case "percent": return "%"
        case "points": return "pts"
        case "rr": return ": 1"
        default: return ""
        }
    }

    /// The stop in one plain sentence, as the trader will see it fire.
    private var stopSentence: String {
        let on = bind.get("risk.stop_loss.on")?.string ?? "premium"
        let type = bind.get("risk.stop_loss.type")?.string ?? "multiple"
        let v = bind.get("risk.stop_loss.value")?.number ?? 0
        let n = v.formatted(.number.precision(.fractionLength(0...2)))
        let sells = (bind.get("legs")?.array ?? []).contains { $0["side"]?.string == "SELL" }
        if on == "underlying" { return "Exit when the index moves \(n) points against the trade." }
        switch type {
        case "multiple":
            return sells ? "Exit a sold leg when its premium reaches \(n)× the entry price (₹40 → ₹\((40 * v).formatted(.number.precision(.fractionLength(0...1)))))."
                         : "Exit a bought leg when its premium falls to \(n)× the entry price."
        case "percent":
            return sells ? "Exit a sold leg when its premium rises \(n)% above entry." : "Exit a bought leg when its premium falls \(n)% below entry."
        default:
            return "Exit a leg when its premium moves \(n) points against it."
        }
    }

    /// A new strategy shows defaults; write them so what is on screen is what gets checked and saved.
    private func writeDefaults() {
        if bind.get("risk.stop_loss") == nil {
            bind.set("risk.stop_loss", .object(["on": .string("premium"), "type": .string("multiple"), "value": .number(2)]))
        }
        if bind.get("risk.max_lots") == nil { bind.set("risk.max_lots", .number(10)) }
        if bind.get("risk.max_trades_per_day") == nil { bind.set("risk.max_trades_per_day", .number(2)) }
    }
}

/// A strike as the trader thinks of it: out of / at / in the money and how many strikes, never a signed number.
/// Calls are out of the money above ATM, puts below; the sign is worked out here.
struct StrikePicker: View {
    let bind: SpecBinding
    let path: String

    private var type: String { bind.get("\(path).type")?.string ?? "CE" }
    private var raw: Int { Int(bind.get("\(path).strike.value")?.number ?? 0) }
    /// + is out of the money for calls (higher strike), - for puts (lower strike).
    private var otmSign: Int { type == "PE" ? -1 : 1 }
    private var money: String { raw == 0 ? "ATM" : (raw * otmSign > 0 ? "OTM" : "ITM") }

    var body: some View {
        Picker("Moneyness", selection: Binding(get: { money }, set: { m in
            let n = max(abs(raw), 1)
            bind.set("\(path).strike.value", .number(Double(m == "ATM" ? 0 : (m == "OTM" ? n : -n) * otmSign)))
        })) { Text("OTM").tag("OTM"); Text("ATM").tag("ATM"); Text("ITM").tag("ITM") }
            .pickerStyle(.segmented)
        if money != "ATM" {
            Stepper("Strikes away: \(abs(raw))", value: Binding(get: { abs(raw) }, set: { n in
                bind.set("\(path).strike.value", .number(Double(n * (money == "OTM" ? otmSign : -otmSign))))
            }), in: 1...20).frame(minHeight: 44)
        }
        Text(result).font(.caption).foregroundStyle(.secondary)
    }

    private var result: String {
        let kind = type == "PE" ? "put" : "call"
        if raw == 0 { return "The \(kind) at the strike nearest the index at entry." }
        let dir = raw > 0 ? "above" : "below"
        return "The \(kind) \(abs(raw)) strike\(abs(raw) == 1 ? "" : "s") \(dir) the ATM strike at entry (\(money))."
    }
}

struct ScheduleStep: View {
    let bind: SpecBinding
    private let days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    var body: some View {
        Form {
            Section("Days") {
                ForEach(days, id: \.self) { d in
                    Toggle(d, isOn: Binding(get: {
                        (bind.get("schedule.days")?.array ?? days.map(JSONValue.string)).contains(.string(d))
                    }, set: { on in
                        var list = (bind.get("schedule.days")?.array ?? days.map(JSONValue.string)).compactMap(\.string)
                        if on { list.append(d) } else { list.removeAll { $0 == d } }
                        bind.set("schedule.days", .array(days.filter(list.contains).map(JSONValue.string)))
                    }))
                }
                Toggle("Skip expiry day", isOn: bind.bool("schedule.skip_expiry_day"))
            }
            Section {
                DatePicker("Start", selection: bind.time("schedule.start", "09:20"), displayedComponents: .hourAndMinute)
                DatePicker("No new entries after", selection: bind.time("schedule.no_new_after", "14:30"), displayedComponents: .hourAndMinute)
                DatePicker("Exit by", selection: bind.time("schedule.exit_by", "15:15"), displayedComponents: .hourAndMinute)
            } header: { Text("Times (IST)") }
            .environment(\.timeZone, Fmt.ist)
            let indexStop = bind.get("risk.stop_loss.on")?.string == "underlying"
            Section {
                Toggle("Hold overnight", isOn: bind.bool("schedule.hold_overnight"))
                    .disabled(indexStop && bind.get("schedule.hold_overnight")?.bool != true)
                if bind.get("schedule.hold_overnight")?.bool == true {
                    DatePicker("Expiry-day exit", selection: bind.time("schedule.expiry_exit_time", "15:15"), displayedComponents: .hourAndMinute)
                        .environment(\.timeZone, Fmt.ist)
                }
            } footer: {
                if indexStop {
                    Text("Not with an index stop: the app holds it, and overnight nothing watches the index. Use a premium stop (Risk) to hold overnight.")
                        .foregroundStyle(bind.get("schedule.hold_overnight")?.bool == true ? Color.tdBad : .secondary)
                } else if bind.get("schedule.hold_overnight")?.bool == true {
                    Text("Positions stay open after Exit by. Each leg's premium stop stays at Zerodha overnight; the trade closes by the expiry-day exit at the latest.")
                } else {
                    Text("Everything closes by Exit by, every day.")
                }
            }
        }
        .navigationTitle("Exit and schedule")
    }
}

struct CostsStep: View {
    let bind: SpecBinding
    var body: some View {
        Form {
            Section {
                NumberField(label: "Capital", value: bind.number("costs.capital", 200000), unit: "₹")
                Text("2% a trade = \(Fmt.rupees(bind.number("costs.capital", 200000).wrappedValue * 0.02)) · 4% a day = \(Fmt.rupees(bind.number("costs.capital", 200000).wrappedValue * 0.04))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                Picker("Fills", selection: bind.text("costs.fill_model", "honest")) {
                    Text("Honest").tag("honest"); Text("Classic (optimistic)").tag("classic")
                }
                NumberField(label: "Slippage", value: bind.number("costs.slippage_pct", 0.5), unit: "%")
                Picker("Stop order", selection: bind.text("costs.sl_order", "assume_fill")) {
                    Text("Assume it fills").tag("assume_fill"); Text("SL-limit: may stay unfilled").tag("limit")
                }
                NumberField(label: "Stop fill inside a bar (0–1)", value: bind.number("costs.stop_fill_frac", 0.5))
                Toggle("Skip strikes too thin to trade", isOn: bind.bool("costs.skip_thin"))
            } header: { Text("Fills") } footer: {
                Text("Honest fills: cheap options pay at least ₹0.10–0.20 a side and no fill uses a trade older than 3 minutes. Every result is net of all charges.")
            }
        }
        .navigationTitle("Costs and fills")
    }
}

/// Python strategies (Algo Lab): the settings the strategy declares, with its defaults; changed ones go in params.
struct ParamsStep: View {
    @Environment(AppModel.self) private var model
    let bind: SpecBinding
    @State private var declared: [JSONValue] = []
    @State private var info: JSONValue?

    var body: some View {
        Form {
            if let d = info?["description"]?.string, !d.isEmpty { Section { Text(d).font(.footnote) } }
            if declared.isEmpty { Text("This strategy has no settings.") }
            ForEach(Array(declared.enumerated()), id: \.offset) { _, p in
                let k = p["name"]?.string ?? ""
                let label = p["label"]?.string ?? humanize(k)
                let current = bind.get("params.\(k)") ?? p["default"]
                switch current {
                case .number?:
                    NumberField(label: label, value: Binding(get: { bind.get("params.\(k)")?.number ?? p["default"]?.number ?? 0 },
                                                             set: { bind.set("params.\(k)", .number($0)) }))
                case .bool?:
                    Toggle(label, isOn: Binding(get: { bind.get("params.\(k)")?.bool ?? p["default"]?.bool ?? false },
                                                set: { bind.set("params.\(k)", .bool($0)) }))
                default:
                    if let choices = p["choices"]?.array, !choices.isEmpty {
                        Picker(label, selection: Binding(get: { bind.get("params.\(k)")?.string ?? p["default"]?.string ?? "" },
                                                         set: { bind.set("params.\(k)", .string($0)) })) {
                            ForEach(choices.compactMap(\.string), id: \.self) { Text($0) }
                        }
                    } else {
                        LabeledContent(label) {
                            TextField(label, text: Binding(get: { bind.get("params.\(k)")?.string ?? p["default"]?.string ?? "" },
                                                           set: { bind.set("params.\(k)", .string($0)) }))
                                .multilineTextAlignment(.trailing)
                        }
                    }
                }
            }
        }
        .navigationTitle("Settings")
        .task {
            guard let m = bind.get("module")?.string else { return }
            info = try? await model.api.raw("GET", "/v1/strategies/python/\(m)/params")
            declared = info?["params"]?.array ?? []
        }
    }
}
