import SwiftUI

/// One shadow or live session: what the strategy holds, its legs at the desk, arming (live: type LIVE, then Face ID,
/// for today only), disarm and stop. A shadow session that ran 5 trading days offers "Move to live".
struct LiveSessionView: View {
    @Environment(AppModel.self) private var model
    let sessionID: Int
    @State private var d: JSONValue?
    @State private var error: String?
    @State private var note: String?
    @State private var busy = false
    @State private var showArm = false
    @State private var confirmStop = false
    @State private var confirmLive = false

    private var isLive: Bool { d?["mode"]?.string == "live" }

    /// Arming is refused outside 09:00–15:20 on weekdays (holidays are checked by the server): say so before Face ID.
    private var armBlocked: String? {
        let t = PaperClock.minutesIST()
        if !(2...6).contains(t.weekday) { return "Arming opens at 09:00 on a trading day." }
        if t.minute < 9 * 60 { return "Arming opens at 09:00 IST." }
        if t.minute >= 15 * 60 + 20 { return "Arming closed for today (until 15:20 IST)." }
        return nil
    }

    var body: some View {
        List {
            if let error {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn)
                    Button("Retry") { Task { await load() } }
                }
            }
            if let note { Section { Label(note, systemImage: "info.circle").font(.footnote) } }
            if let d {
                Section {
                    HStack {
                        StatusPill(text: isLive ? "LIVE" : "SHADOW", systemImage: isLive ? "bolt.fill" : "eye",
                                   color: isLive ? .tdLive : .tdPaper)
                        if isLive {
                            StatusPill(text: d["armed_today"]?.bool == true ? "Armed today" : "Not armed",
                                       systemImage: d["armed_today"]?.bool == true ? "bolt.fill" : "bolt.slash",
                                       color: d["armed_today"]?.bool == true ? .tdLive : .secondary)
                        }
                        Spacer()
                        Text(humanize(d["status"]?.string ?? "")).font(.subheadline.weight(.semibold))
                    }
                    if let e = d["state"]?["engine"], e != .null {
                        PnlLine(label: "Strategy model today\(e["as_of"]?.string.map { " · as of \($0) IST" } ?? "")",
                                value: e["day_pnl"]?.number ?? 0, big: true, mode: isLive ? "Live" : "Shadow")
                        Text("The strategy's own model. Real fills and P&L at Zerodha are on the Orders desk.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if let m = d["max_loss_per_day"]?.number {
                        LabeledContent("Max loss a day", value: Fmt.rupees(m)).monospacedDigit()
                    }
                    if let err = d["error"]?.string { Label(err, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Color.tdWarn) }
                    if !isLive {
                        Text("Shadow: real Zerodha prices, simulated orders. Nothing is sent. \(Int(d["state"]?["days"]?.number ?? 0)) of 5 trading days done.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                let legs = d["state"]?["legs"]?.array ?? []
                Section("Legs at the desk") {
                    if legs.isEmpty { Text("No legs today.").foregroundStyle(.secondary) }
                    ForEach(Array(legs.enumerated()), id: \.offset) { _, lg in DeskLegRow(lg: lg) }
                }
                if d["status"]?.string != "stopped" { actions(d) }
            } else if error == nil {
                ProgressView()
            }
        }
        .navigationTitle(d?["name"]?.string ?? "Session")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showArm) {
            ArmSheet(sessionID: sessionID, name: d?["name"]?.string ?? "",
                     maxLoss: d?["max_loss_per_day"]?.number) { result in
                showArm = false
                if let result { note = result }
                Task { await load() }
            }
        }
        .confirmationDialog("Stop this session?", isPresented: $confirmStop, titleVisibility: .visible) {
            Button("Stop and exit its legs", role: .destructive) { Task { await post("stop") } }
        } message: {
            Text("It is disarmed, its legs exit (each stop order at Zerodha becomes the exit), and it ends. It can't be restarted.")
        }
        .confirmationDialog("Create a live session from this shadow session?", isPresented: $confirmLive, titleVisibility: .visible) {
            Button("Create live session") { Task { await moveToLive() } }
        } message: {
            Text("Same frozen settings. It starts unarmed: nothing is sent until you arm it on a trading day with Face ID.")
        }
        .refreshable { await load() }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    @ViewBuilder private func actions(_ d: JSONValue) -> some View {
        Section {
            if isLive {
                if d["armed_today"]?.bool == true {
                    Button { Task { await post("disarm") } } label: {
                        Label("Disarm (no new entries)", systemImage: "bolt.slash").frame(minHeight: 44)
                    }
                } else {
                    Button { showArm = true } label: {
                        Label("Arm for today…", systemImage: "bolt.fill").frame(minHeight: 44)
                    }
                    .tint(Color.tdLive)
                    .disabled(armBlocked != nil)
                    if let why = armBlocked { Text(why).font(.caption).foregroundStyle(.secondary) }
                }
            } else if Int(d["state"]?["days"]?.number ?? 0) >= 5 {
                Button { confirmLive = true } label: {
                    Label("Move to live…", systemImage: "arrow.right.circle").frame(minHeight: 44)
                }
                .tint(Color.tdLive)
            }
            Button(role: .destructive) { confirmStop = true } label: {
                Label("Stop session", systemImage: "stop.fill").frame(minHeight: 44)
            }
        } footer: {
            Text(isLive ? "Arming lasts for today only. Disarming and stopping always work, from any device." : "")
        }
        .disabled(busy)
    }

    private func load() async {
        do { d = try await model.api.raw("GET", "/v1/live/sessions/\(sessionID)"); error = nil }
        catch { self.error = error.localizedDescription }
    }

    private func post(_ what: String) async {
        busy = true; defer { busy = false }
        do {
            let r = try await model.api.raw("POST", "/v1/live/sessions/\(sessionID)/\(what)")
            note = r["note"]?.string
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func moveToLive() async {
        busy = true; defer { busy = false }
        do {
            let r = try await model.api.raw("POST", "/v1/live/sessions",
                                            body: JSONValue.object(["mode": .string("live"), "shadow_id": .number(Double(sessionID))]))
            note = r["note"]?.string
        } catch { self.error = error.localizedDescription }
    }
}

/// Arm a live session for today: type LIVE, then Face ID signs a one-time challenge for this session and day.
struct ArmSheet: View {
    @Environment(AppModel.self) private var model
    let sessionID: Int
    let name: String
    let maxLoss: Double?
    var done: (String?) -> Void
    @State private var typed = ""
    @State private var error: String?
    @State private var sending = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label("Real orders", systemImage: "bolt.fill").font(.headline).foregroundStyle(Color.tdLive)
                    Text("\(name) may send real orders to Zerodha today, from the server, within your account limits\(maxLoss.map { " and its max loss of \(Fmt.rupees($0)) a day" } ?? ""). Every entry gets a stop order at Zerodha.")
                        .font(.subheadline)
                }
                Section {
                    TextField("Type LIVE", text: $typed)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                        .font(.title3.monospaced())
                } footer: { Text("Then confirm with Face ID. Arming ends at the close today.") }
                if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdBad) }
                Section {
                    Button {
                        Task { await arm() }
                    } label: {
                        HStack { Text("Arm with Face ID").font(.headline); if sending { Spacer(); ProgressView() } }
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.borderedProminent).tint(Color.tdLive)
                    .disabled(typed != "LIVE" || sending)
                }
            }
            .navigationTitle("Arm live")
            .navigationBarTitleDisplayMode(.inline)
            .interactiveDismissDisabled(sending)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { done(nil) } } }
        }
    }

    private func arm() async {
        sending = true; defer { sending = false }
        do {
            let ch = try await model.api.raw("POST", "/v1/live/sessions/\(sessionID)/arm/challenge")
            guard let cid = ch["challenge_id"]?.string, let nonce = ch["nonce"]?.string, let day = ch["day"]?.string else {
                error = "The server did not send a challenge."; return
            }
            let sig = try model.sign(SignMessage.arm(challengeID: cid, nonce: nonce, sessionID: sessionID, day: day),
                                     reason: "Arm live trading for today")
            let r = try await model.api.raw("POST", "/v1/live/sessions/\(sessionID)/arm", body: JSONValue.object(
                ["challenge_id": .string(cid), "signature": .string(sig), "typed": .string(typed)]))
            done(r["note"]?.string ?? "Armed for today.")
        } catch { self.error = error.localizedDescription }
    }
}

/// Account limits for live and shadow: required before either can run.
struct LimitsView: View {
    @Environment(AppModel.self) private var model
    var done: () -> Void
    @State private var maxLots = 1
    @State private var maxOrders = 10
    @State private var dailyLoss = 5000.0
    @State private var error: String?
    @State private var saving = false
    @State private var loaded = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Stepper("Max lots an order: \(maxLots)", value: $maxLots, in: 1...50).frame(minHeight: 44)
                    Stepper("Max opening orders a day: \(maxOrders)", value: $maxOrders, in: 1...100).frame(minHeight: 44)
                    NumberField(label: "Max loss a day", value: $dailyLoss, unit: "₹")
                } footer: {
                    Text("For the whole account, on top of each strategy's own limits (the tighter one applies). At the day loss every leg exits and live locks until tomorrow.")
                }
                if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdBad) }
            }
            .navigationTitle("Account limits")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { done() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task { await save() } }.disabled(saving || !loaded || dailyLoss <= 0)
                }
            }
            .task {
                do {
                    let l = try await model.api.raw("GET", "/v1/live/limits")["limits"]
                    if let l, l != .null {
                        maxLots = Int(l["max_lots"]?.number ?? 1)
                        maxOrders = Int(l["max_orders_day"]?.number ?? 10)
                        dailyLoss = l["daily_loss"]?.number ?? 5000
                    }
                    loaded = true                      // never save over limits that failed to load
                } catch { self.error = "Couldn't load your limits: \(error.localizedDescription)" }
            }
        }
    }

    private func save() async {
        saving = true; defer { saving = false }
        do {
            _ = try await model.api.raw("PUT", "/v1/live/limits", body: JSONValue.object(
                ["max_lots": .number(Double(maxLots)), "max_orders_day": .number(Double(maxOrders)), "daily_loss": .number(dailyLoss)]))
            done()
        } catch { self.error = error.localizedDescription }
    }
}
