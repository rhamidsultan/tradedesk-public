import SwiftUI

/// Option chain for one expiry on one stored day, as of a chosen minute. Calls left, puts right.
struct ChainView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hideAmounts = false
    let underlying: String
    let expiry: String
    let day: String
    @State private var at = "15:29"
    @State private var chain: OptionChain?
    @State private var error: String?
    @State private var showOI = false

    // to the F&O close: 15:29 until 31 Jul 2026, 15:39 from 3 Aug 2026 (closing auction rules)
    private var times: [String] {
        let base = ["09:20", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "15:15", "15:29"]
        return day >= "2026-08-03" ? base + ["15:35", "15:39"] : base
    }

    var body: some View {
        ScrollViewReader { proxy in
        List {
            Section {
                Picker("As of (IST)", selection: $at) { ForEach(times, id: \.self) { Text($0) } }
                Toggle("Show open interest", isOn: $showOI)
                if let s = chain?.spot { LabeledContent("\(underlying) at \(at)", value: String(format: "%.2f", s)) }
            }
            if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            if let c = chain {
                Section {
                    HStack {
                        Text("Calls").frame(maxWidth: .infinity, alignment: .leading)
                        Text("Strike").frame(width: 76)
                        Text("Puts").frame(maxWidth: .infinity, alignment: .trailing)
                    }
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    ForEach(c.strikes) { r in row(r, atm: r.strike == atmStrike(c)).id(r.strike) }
                } footer: {
                    Text("Last traded price at or before \(at) IST from stored 1-minute bars; a dash means no trade yet that day.")
                }
            }
        }
        .onChange(of: chain) { _, c in
            if let c, let a = atmStrike(c) { proxy.scrollTo(a, anchor: .center) }
        }
        }
        .navigationTitle("Expiry \(Fmt.marketDay(expiry))")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: at) { await load() }
    }

    private func atmStrike(_ c: OptionChain) -> Double? {
        guard let s = c.spot else { return nil }
        return c.strikes.min(by: { abs($0.strike - s) < abs($1.strike - s) })?.strike
    }

    private func row(_ r: OptionChain.Row, atm: Bool) -> some View {
        HStack {
            side(r.CE, symbol: "CE", strike: r.strike, align: .leading)
            VStack(spacing: 0) {
                Text(Fmt.rupees(r.strike).replacingOccurrences(of: "₹", with: ""))
                    .font(.subheadline.monospacedDigit().weight(atm ? .bold : .regular))
                if atm { Text("ATM").font(.caption2.weight(.bold)).foregroundStyle(.tint) }
            }
            .frame(width: 76)
            side(r.PE, symbol: "PE", strike: r.strike, align: .trailing)
        }
        .listRowBackground(atm ? Color.accentColor.opacity(0.08) : nil)
    }

    @ViewBuilder private func side(_ s: OptionChain.Side?, symbol: String, strike: Double, align: Alignment) -> some View {
        if let s, let ltp = s.ltp {
            Button { model.morePath.append(.contract(underlying, contractSymbol(strike, symbol), day)) } label: {
                VStack(alignment: align == .leading ? .leading : .trailing, spacing: 1) {
                    Text(String(format: "%.2f", ltp)).font(.subheadline.monospacedDigit())
                    if showOI, let oi = s.oi { Text("OI " + Fmt.rupees(Double(oi)).replacingOccurrences(of: "₹", with: "")).font(.caption2.monospacedDigit()).foregroundStyle(.secondary) }
                }
                .frame(maxWidth: .infinity, alignment: align)
                .contentShape(.rect)
            }
            .buttonStyle(.borderless)
            .accessibilityLabel("\(symbol == "CE" ? "Call" : "Put") \(Int(strike)), \(String(format: "%.2f", ltp)). Open chart")
        } else {
            Text("—").foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: align)
        }
    }

    /// Store symbol: NIFTY + yymmdd + strike + CE/PE (e.g. NIFTY26102025000CE).
    private func contractSymbol(_ strike: Double, _ type: String) -> String {
        let e = expiry.replacingOccurrences(of: "-", with: "").dropFirst(2)
        let k = strike == strike.rounded() ? String(Int(strike)) : String(strike)
        return "\(underlying)\(e)\(k)\(type)"
    }

    private func load() async {
        do { chain = try await model.api.get("/v1/data/\(underlying)/chain?expiry=\(expiry)&day=\(day)&at=\(at)"); error = nil }
        catch { self.error = error.localizedDescription }
    }
}
