import SwiftUI

/// The risk step: loss per trade at the stop, target, charges, lots the capital carries, and for strangles the
/// worst case with both legs stopped past their trigger, against the 2% and 4% marks. Anything above the marks
/// must be accepted in ₹ to save. Amounts always show here, even with "hide amounts" on: this is a risk decision.
/// The spec reviewed is sent with the save; the server refuses if the draft changed meanwhile.
struct RiskReview: View {
    @Environment(AppModel.self) private var model
    let strategyID: Int
    let spec: JSONValue
    let ready: Bool
    var saved: () -> Void
    @State private var r: JSONValue?
    @State private var note = ""
    @State private var accept = false
    @State private var message: String?
    @State private var saving = false

    var body: some View {
        Form {
            if let r, r["ok"]?.bool == true {
                Section("One trade at its stop") {
                    ForEach(Array((r["legs"]?.array ?? []).enumerated()), id: \.offset) { _, l in
                        LabeledContent("\(l["side"]?.string ?? "") \(l["type"]?.string ?? "") · \(Int(l["qty"]?.number ?? 0)) qty @ \(money(l["premium"]?.number, decimals: 2))") {
                            Text(money(l["loss_at_stop"]?.number.map { -$0 })).foregroundStyle(Color.tdBad)
                        }
                    }
                    LabeledContent("Charges", value: money(r["charges_per_trade"]?.number.map { -$0 }, decimals: 2))
                    LabeledContent("Loss per trade") {
                        Text(money(r["loss_per_trade"]?.number.map { -$0 })).font(.headline).foregroundStyle(Color.tdBad)
                    }
                    if let pct = r["loss_pct_of_capital"]?.number {
                        meter(pct / 2.0, label: "\(pct.formatted())% of capital · budget 2% (\(money(r["budget_2pct"]?.number)))")
                    }
                    if (r["unknown_premiums"]?.number ?? 0) > 0 {
                        Label("Some premiums are unknown (no stored chain yet): the loss shown is incomplete.", systemImage: "questionmark.circle")
                            .font(.caption).foregroundStyle(Color.tdWarn)
                    }
                }
                .monospacedDigit()
                Section("Day and capital") {
                    LabeledContent("Capital", value: money(r["capital"]?.number))
                    LabeledContent("Max loss a day", value: money(r["max_loss_per_day"]?.number))
                    LabeledContent("4% of capital", value: money(r["day_mark_4pct"]?.number))
                    if let lots = r["lots_capital_carries"]?.number {
                        LabeledContent("Lots the capital carries", value: "\(Int(lots)) (margin ≈ \(money(r["margin_per_lot"]?.number)) a lot)")
                    }
                    LabeledContent("Lot size", value: r["lot_size"]?.number.map { "\(Int($0))" } ?? "—")
                }
                .monospacedDigit()
                if let worst = r["app_down_worst"]?.number {
                    Section {
                        LabeledContent("Both legs stopped past the trigger") { Text(money(-worst)).foregroundStyle(Color.tdBad) }
                        meter((r["app_down_vs_2pct"]?.number ?? 0), label: "\((r["app_down_vs_2pct"]?.number ?? 0).formatted())× the 2% budget")
                        meter((r["app_down_vs_4pct"]?.number ?? 0), label: "\((r["app_down_vs_4pct"]?.number ?? 0).formatted())× the 4% day mark")
                    } header: { Text("Strangle worst case") } footer: {
                        Text("If both stops trigger in a spike, SL-limit orders fill up to their buffer beyond the trigger. Stops sit at Zerodha, so this holds even if the app or server is down.")
                    }
                    .monospacedDigit()
                }
                Section {
                    TextField("What changed (optional)", text: $note)
                    if let need = r["accept_required"]?.number {
                        ForEach(Array((r["acceptItems"]?.array ?? r["accept_items"]?.array ?? []).enumerated()), id: \.offset) { _, i in
                            LabeledContent(i["what"]?.string ?? "") {
                                Text(money(i["amount"]?.number)).foregroundStyle(Color.tdBad)
                            }
                        }
                        Toggle(isOn: $accept) {
                            Text("I accept losses of up to \(money(need)), above the 2% / 4% marks")
                        }
                    }
                    Button {
                        Task { await save() }
                    } label: {
                        HStack { Text("Save new version").font(.headline); if saving { Spacer(); ProgressView() } }
                    }
                    .disabled(!ready || saving || (r["accept_required"]?.number != nil && !accept))
                }
            } else if let r {
                ForEach(Array((r["problems"]?.array ?? []).enumerated()), id: \.offset) { _, p in
                    Label("\(p["where"]?.string ?? ""): \(p["text"]?.string ?? "")", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(Color.tdBad)
                }
            } else {
                ProgressView()
            }
            if let message { Text(message).font(.footnote) }
        }
        .navigationTitle("Risk check")
        .task { await load() }
    }

    private func money(_ v: Double?, decimals: Int = 0) -> String {
        v.map { Fmt.rupees($0, decimals: decimals) } ?? "—"
    }

    private func meter(_ fraction: Double, label: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Gauge(value: min(fraction, 1.5), in: 0...1.5) { EmptyView() }
                .gaugeStyle(.accessoryLinear)
                .tint(fraction > 1 ? Color.tdBad : fraction > 0.75 ? Color.tdWarn : Color.tdGood)
            Text(label + (fraction > 1 ? " — over" : "")).font(.caption)
        }
        .accessibilityElement(children: .combine)
    }

    private func load() async {
        struct B: Encodable { let spec: JSONValue }
        do { r = try await model.api.raw("POST", "/v1/strategies/risk", body: B(spec: spec)) }
        catch { message = error.localizedDescription }
    }

    private func save() async {
        saving = true; defer { saving = false }
        struct B: Encodable { let note: String; let accept_risk: Double?; let reviewed: JSONValue }
        do {
            let out = try await model.api.raw("POST", "/v1/strategies/\(strategyID)/save",
                                              body: B(note: note, accept_risk: accept ? r?["accept_required"]?.number : nil,
                                                      reviewed: spec))
            message = "Saved as version \(Int(out["version"]?.number ?? 0))."
            saved()
        } catch {
            message = error.localizedDescription
            accept = false
            await load()        // the draft or the amounts may have changed: show the current risk again
        }
    }
}
