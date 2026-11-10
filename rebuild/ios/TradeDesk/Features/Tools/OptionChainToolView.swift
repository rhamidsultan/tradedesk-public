import SwiftUI

/// One option leg picked on the chain or typed in the analyser.
struct ToolLeg: Identifiable, Hashable {
    var id = UUID()
    var type: String          // CE | PE
    var strike: Double
    var side: String          // BUY | SELL
    var lots: Int = 1
    var expiry: String        // yyyy-MM-dd
    var price: Double?        // nil: the server takes the stored price at that minute
    var lotSize: Int?         // for "1 lot (65 qty)"

    var json: JSONValue {
        var o: [String: JSONValue] = ["type": .string(type), "strike": .number(strike), "side": .string(side),
                                      "lots": .number(Double(lots)), "expiry": .string(expiry)]
        if let price { o["price"] = .number(price) }
        return .object(o)
    }

    var lotsText: String {
        "\(lots) lot\(lots == 1 ? "" : "s")" + (lotSize.map { " (\(lots * $0) qty)" } ?? "")
    }

    var title: String { "\(side == "BUY" ? "Buy" : "Sell") \(lotsText) · \(Int(strike)) \(type) · \(Fmt.marketDay(expiry))" }
}

/// The most dangerous mode wins when several sessions hold the same contract: LIVE > SHADOW > PAPER.
enum HeldMode: Int, Comparable {
    case paper = 0, shadow, live
    init?(_ s: String?) {
        switch s { case "live": self = .live; case "shadow": self = .shadow; case "paper": self = .paper; default: return nil }
    }
    static func < (a: HeldMode, b: HeldMode) -> Bool { a.rawValue < b.rawValue }
    var word: String { ["PAPER", "SHADOW", "LIVE"][rawValue] }
    var color: Color { [Color.tdPaper, Color.secondary, Color.tdLive][rawValue] }
}

/// Trade › Option chain: OI, change in OI since the previous day, IV, build-up, PCR, max pain, ATM IV and the
/// straddle, with the user's held legs marked by mode. Tap a price to add that leg to a basket for the analyser.
struct OptionChainToolView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("toolsUnderlying") private var underlying = "NIFTY"
    @State private var expiry: String?            // what the picker asks for (nil = nearest)
    @State private var chain: JSONValue?          // what is on screen
    @State private var error: String?
    @State private var loading = false
    @State private var basket: [ToolLeg] = []
    @State private var picking: (type: String, strike: Double, ltp: Double?)?
    @State private var showOI = true
    @State private var analyseLegs: [ToolLeg]?
    @State private var lastATM: String?

    static let underlyings = ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"]

    /// The rows on screen belong to what the pickers show (not mid-load or after a failed load for another expiry).
    private var current: Bool {
        guard let c = chain, c["underlying"]?.string == underlying else { return false }
        return expiry == nil || c["expiry"]?.string == expiry
    }

    var body: some View {
        ScrollViewReader { proxy in
            List {
                Section {
                    Picker("Index", selection: $underlying) { ForEach(Self.underlyings, id: \.self) { Text($0) } }
                    if let exps = chain?["expiries"]?.array?.compactMap(\.string), !exps.isEmpty, chain?["underlying"]?.string == underlying {
                        Picker("Expiry", selection: Binding(get: { expiry ?? exps[0] }, set: { expiry = $0 })) {
                            ForEach(exps, id: \.self) { Text(Fmt.marketDay($0)) }
                        }
                    }
                    Toggle("Open interest columns", isOn: $showOI)
                }
                if let error {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn)
                        Button("Retry") { Task { await load() } }
                    }
                }
                if let c = chain {
                    summary(c)
                    let held = c["held"]?.array ?? []
                    if !held.isEmpty { heldSection(held) }
                    Section {
                        ForEach(Array((c["rows"]?.array ?? []).enumerated()), id: \.offset) { _, r in
                            row(r, atm: r["strike"]?.number == c["atm"]?.number, held: held).id(r["strike"]?.number ?? 0)
                        }
                    } header: {
                        VStack(spacing: 4) {
                            if !current { Label("Loading \(expiry.map { Fmt.marketDay($0) } ?? underlying)…", systemImage: "hourglass").font(.caption) }
                            header
                        }
                    } footer: {
                        Text("Prices at or before \(c["data_as_of"]?.string ?? c["at"]?.string ?? "") IST on \(Fmt.marketDay(c["date"]?.string)) from stored 1-minute bars. OI change and build-up compare with the close of \(Fmt.marketDay(c["prev_day"]?.string)). IV from Black-Scholes. Tap a price to add the leg.")
                    }
                    .disabled(!current)
                    .opacity(current ? 1 : 0.4)
                } else if loading {
                    ProgressView().frame(maxWidth: .infinity)
                }
            }
            .listStyle(.plain)
            .onChange(of: chain) { _, c in
                // jump to ATM only when the index, expiry or ATM changes, not on every refresh
                let k = "\(c?["underlying"]?.string ?? "")|\(c?["expiry"]?.string ?? "")|\(c?["atm"]?.number ?? 0)"
                if k != lastATM, let a = c?["atm"]?.number { lastATM = k; proxy.scrollTo(a, anchor: .center) }
            }
        }
        .navigationTitle("Option chain")
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) { if !basket.isEmpty { basketBar } }
        .confirmationDialog(pickTitle, isPresented: Binding(get: { picking != nil }, set: { if !$0 { picking = nil } }),
                            titleVisibility: .visible) {
            Button("Buy") { add("BUY") }
            Button("Sell") { add("SELL") }
        }
        .navigationDestination(isPresented: Binding(get: { analyseLegs != nil }, set: { if !$0 { analyseLegs = nil } })) {
            AnalyserView(underlying: underlying, day: chain?["date"]?.string, legs: analyseLegs ?? [],
                         expiries: chain?["expiries"]?.array?.compactMap(\.string) ?? [], atm: chain?["atm"]?.number)
        }
        .refreshable { await load() }
        .task(id: "\(underlying)|\(expiry ?? "")") {
            // every 30 s while on screen and the market is open
            while !Task.isCancelled {
                await load()
                let open = chain?["freshness"]?.string != "closed"
                try? await Task.sleep(for: .seconds(open ? 30 : 600))
            }
        }
        .onChange(of: underlying) { _, _ in expiry = nil; basket = [] }
    }

    private var pickTitle: String {
        guard let p = picking else { return "" }
        let e = chain?["expiry"]?.string.map { " · " + Fmt.marketDay($0) } ?? ""
        return "\(Int(p.strike)) \(p.type)\(e)" + (p.ltp.map { " at \(String(format: "%.2f", $0))" } ?? "")
    }

    private func add(_ side: String) {
        // the expiry of the rows on screen, never the picker's (it may be mid-load)
        guard let p = picking, current, let e = chain?["expiry"]?.string else { picking = nil; return }
        basket.append(ToolLeg(type: p.type, strike: p.strike, side: side, expiry: e,
                              lotSize: chain?["lot_size"]?.number.map(Int.init)))
        picking = nil
    }

    private var basketBar: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(basket.count) leg\(basket.count == 1 ? "" : "s") · 1 lot each").font(.subheadline.weight(.semibold))
                Text(basket.map { "\($0.side == "BUY" ? "Buy" : "Sell") \(Int($0.strike))\($0.type)" }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Button("Clear", role: .destructive) { basket = [] }.buttonStyle(.bordered).controlSize(.large)
            Button("Analyse") { analyseLegs = basket }.buttonStyle(.borderedProminent).controlSize(.large)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
        .background(.regularMaterial)
    }

    @ViewBuilder private func freshness(_ c: JSONValue) -> some View {
        switch c["freshness"]?.string {
        case "live": StatusPill(text: "Live · \(c["data_as_of"]?.string ?? "") IST", systemImage: "dot.radiowaves.left.and.right", color: .tdGood)
        case "stale": StatusPill(text: "Stale · prices from \(c["data_as_of"]?.string ?? "—") IST", systemImage: "clock.badge.exclamationmark", color: .tdWarn)
        default: StatusPill(text: "Market closed · \(Fmt.marketDay(c["date"]?.string)), \(c["data_as_of"]?.string ?? c["at"]?.string ?? "") IST",
                            systemImage: "moon", color: .secondary)
        }
    }

    private func summary(_ c: JSONValue) -> some View {
        Section {
            freshness(c)
            LabeledContent("\(underlying) at \(c["data_as_of"]?.string ?? c["at"]?.string ?? "") IST", value: num(c["spot"]?.number))
            LabeledContent("ATM strike · ATM IV", value: "\(num(c["atm"]?.number, 0)) · \(pct(c["atm_iv"]?.number))")
            LabeledContent("ATM straddle", value: num(c["straddle"]?.number))
            LabeledContent("PCR (put OI ÷ call OI)", value: num(c["pcr"]?.number))
            LabeledContent("Max pain", value: num(c["max_pain"]?.number, 0))
            LabeledContent("Days to expiry", value: num(c["days_to_expiry"]?.number, 1))
            if let lot = c["lot_size"]?.number { LabeledContent("Lot size", value: "\(Int(lot)) qty") }
        } header: { Text("\(Fmt.marketDay(c["date"]?.string)) · expiry \(Fmt.marketDay(c["expiry"]?.string))") }
    }

    private func heldLegs(_ held: [JSONValue], mode: String) -> [ToolLeg] {
        held.filter { $0["source"]?.string == mode }.compactMap { h in
            guard let k = h["strike"]?.number, let t = h["type"]?.string, let e = chain?["expiry"]?.string else { return nil }
            return ToolLeg(type: t, strike: k, side: h["side"]?.string ?? "SELL", lots: Int(h["lots"]?.number ?? 1), expiry: e,
                           price: h["entry"]?.number, lotSize: chain?["lot_size"]?.number.map(Int.init))
        }
    }

    private func heldSection(_ held: [JSONValue]) -> some View {
        Section {
            ForEach(Array(held.enumerated()), id: \.offset) { _, h in
                let m = HeldMode(h["source"]?.string)
                HStack {
                    modePill(h["source"]?.string ?? "")
                    VStack(alignment: .leading, spacing: 1) {
                        Text("\(h["side"]?.string == "SELL" ? "Sell" : "Buy") \(Int(h["strike"]?.number ?? 0)) \(h["type"]?.string ?? "")")
                        Text("\(Int(h["lots"]?.number ?? 0)) lot · \(Int(h["qty"]?.number ?? 0)) qty @ \(num(h["entry"]?.number))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(h["name"]?.string ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(m?.word ?? h["source"]?.string ?? ""): \(h["side"]?.string == "SELL" ? "sell" : "buy") \(Int(h["strike"]?.number ?? 0)) \(h["type"]?.string ?? ""), \(h["name"]?.string ?? "")")
            }
            ForEach(["live", "shadow", "paper"], id: \.self) { mode in
                let legs = heldLegs(held, mode: mode)
                if !legs.isEmpty {
                    Button { analyseLegs = legs } label: {
                        Label("Analyse my \(HeldMode(mode)?.word ?? mode) position", systemImage: "chart.xyaxis.line")
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                }
            }
        } header: { Text("Your legs on this expiry") }
    }

    @ViewBuilder private func modePill(_ s: String) -> some View {
        switch s {
        case "live": StatusPill(text: "LIVE", systemImage: "bolt.fill", color: .tdLive)
        case "shadow": StatusPill(text: "SHADOW", systemImage: "eye", color: .secondary)
        case "paper": StatusPill(text: "PAPER", systemImage: "doc.text", color: .tdPaper)
        default: StatusPill(text: s.uppercased(), systemImage: "questionmark", color: .secondary)
        }
    }

    private var header: some View {
        HStack {
            Text(showOI ? "CALLS · OI · chg" : "CALLS · IV").frame(maxWidth: .infinity, alignment: .leading)
            Text("LTP").frame(width: 58, alignment: .trailing)
            Text("Strike").frame(width: 60)
            Text("LTP").frame(width: 58, alignment: .leading)
            Text(showOI ? "chg · OI · PUTS" : "IV · PUTS").frame(maxWidth: .infinity, alignment: .trailing)
        }
        .font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
        .padding(.vertical, 4)
    }

    private func heldMode(_ held: [JSONValue], _ k: Double, _ t: String) -> HeldMode? {
        held.filter { $0["strike"]?.number == k && $0["type"]?.string == t }.compactMap { HeldMode($0["source"]?.string) }.max()
    }

    private func row(_ r: JSONValue, atm: Bool, held: [JSONValue]) -> some View {
        let k = r["strike"]?.number ?? 0
        return HStack(spacing: 4) {
            cell(r["CE"], type: "CE", strike: k, held: heldMode(held, k, "CE"), leading: true)
            VStack(spacing: 0) {
                Text("\(Int(k))").font(.subheadline.weight(atm ? .bold : .regular)).monospacedDigit()
                if atm { Text("ATM").font(.caption2).foregroundStyle(.tint) }
            }
            .frame(width: 60)
            cell(r["PE"], type: "PE", strike: k, held: heldMode(held, k, "PE"), leading: false)
        }
        .listRowBackground(atm ? Color.accentColor.opacity(0.08) : nil)
    }

    private func cell(_ x: JSONValue?, type: String, strike: Double, held: HeldMode?, leading: Bool) -> some View {
        let ltp = x?["ltp"]?.number
        let oiText = x?["oi"]?.number.map(compact) ?? "—"
        let ch = x?["oi_change"]?.number.map { ($0 >= 0 ? "+" : "") + compact($0) } ?? "—"
        let info = showOI ? (leading ? "\(oiText) · \(ch)" : "\(ch) · \(oiText)") : pct(x?["iv"]?.number)
        let tag = (held.map { "● \($0.word) " } ?? "") + short(x?["buildup"]?.string)
        let infoView = VStack(alignment: leading ? .leading : .trailing, spacing: 1) {
            Text(info).font(.caption2).monospacedDigit().foregroundStyle(.secondary).lineLimit(1)
            if !tag.isEmpty {
                Text(tag).font(.caption2.weight(held != nil ? .semibold : .regular))
                    .foregroundStyle(held?.color ?? .secondary).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: leading ? .leading : .trailing)
        let price = Text(num(ltp)).font(.subheadline.weight(.semibold)).monospacedDigit().lineLimit(1)
            .frame(width: 58, alignment: leading ? .trailing : .leading)
        return Button {
            picking = (type, strike, ltp)
        } label: {
            HStack(spacing: 4) {
                if leading {
                    infoView
                    price
                } else {
                    price
                    infoView
                }
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(x == nil || x == .null)
        .accessibilityLabel("\(Int(strike)) \(type == "CE" ? "call" : "put"), price \(num(ltp)), open interest \(oiText), change \(ch), IV \(pct(x?["iv"]?.number))"
                            + (held.map { ", you hold this in \($0.word)" } ?? "") + ". \(x?["buildup"]?.string ?? "")")
        .accessibilityHint("Add to the analyser basket")
    }

    private func short(_ b: String?) -> String {
        switch b {
        case "long build-up": return "Long build-up"
        case "short build-up": return "Short build-up"
        case "short covering": return "Short covering"
        case "long unwinding": return "Long unwinding"
        default: return ""
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        let asked = underlying
        var q = "/v1/tools/chain?underlying=\(asked)"
        if let expiry { q += "&expiry=\(expiry)" }
        do {
            let c = try await model.api.raw("GET", q)
            guard asked == underlying else { return }          // the index changed while loading
            chain = c
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = "Couldn't load the chain: \(error.localizedDescription)"
        }
    }
}

func num(_ v: Double?, _ dp: Int = 2) -> String {
    guard let v else { return "—" }
    return v.formatted(.number.precision(.fractionLength(dp)).locale(Locale(identifier: "en_IN")))
}

func pct(_ v: Double?) -> String { v.map { String(format: "%.1f%%", $0) } ?? "—" }

/// Indian units: 1.2L (lakh), 1.2Cr (crore); below a lakh in thousands.
func compact(_ v: Double) -> String {
    let a = abs(v), s = v < 0 ? "-" : ""
    if a >= 1e7 { return s + String(format: "%.1fCr", a / 1e7) }
    if a >= 1e5 { return s + String(format: "%.1fL", a / 1e5) }
    if a >= 1e3 { return s + String(format: "%.1fK", a / 1e3) }
    return s + String(Int(a))
}
