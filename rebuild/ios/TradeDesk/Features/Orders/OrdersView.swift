import SwiftUI
import UIKit

/// Orders: LIVE (magenta) or SHADOW (simulated) desk, the pre-open checklist, legs with their stop at Zerodha, what
/// each command actually did, and Stop all pinned at the bottom. Real orders are sent only by the server.
struct OrdersView: View {
    @Environment(AppModel.self) private var model
    @State private var desk: JSONValue?
    @State private var checklist: JSONValue?
    @State private var sessions: [JSONValue] = []
    @State private var loadError: String?
    @State private var loadedAt: Date?
    @State private var lastFailed = false
    @State private var confirmStopAll = false
    @State private var stopping = false
    @State private var stopAllID: String?
    @State private var stopAllOutcome: StopAllOutcome?
    @State private var showLimits = false
    @State private var legSheet: JSONValue?
    @State private var pending: [String: PendingCommand] = [:]     // command id -> what it is and what came back

    struct StopAllOutcome: Equatable {
        var ok: Bool
        var text: String
    }

    struct PendingCommand: Equatable {
        let legID: Int
        let what: String
        var status = "queued"
        var result = ""
    }

    private var mode: String { model.ordersMode }
    private var modeColor: Color { mode == "live" ? .tdLive : .tdPaper }
    private var runnerDown: Bool { desk?["runner_ok"]?.bool == false }
    private var stale: Bool {
        guard let at = updatedAt else { return lastFailed }
        return lastFailed || Date.now.timeIntervalSince(at) > 15
    }
    private var updatedAt: Date? {
        desk?["updated_at"]?.string.flatMap { ISO8601DateFormatter.flexible.date(from: $0) ?? ISO8601DateFormatter().date(from: $0) }
    }

    var body: some View {
        @Bindable var model = model
        NavigationStack {
            List {
                if runnerDown { runnerDownCard }
                if let o = stopAllOutcome, !o.ok { stopAllFailedCard(o) }
                Picker("Mode", selection: $model.ordersMode) {
                    Text("LIVE").tag("live"); Text("SHADOW").tag("shadow")
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
                if let loadError {
                    Section {
                        Label(loadError, systemImage: "wifi.exclamationmark").foregroundStyle(Color.tdWarn)
                        Button("Retry loading") { Task { await load() } }
                    }
                }
                if mode == "live", let c = checklist { checklistSection(c) }
                if let d = desk { deskSections(d) } else if loadError == nil { ProgressView() }
                sessionsSection
                if let o = stopAllOutcome, o.ok { Section { Label(o.text, systemImage: "checkmark.circle").font(.footnote) } }
            }
            .navigationTitle(mode == "live" ? "Orders · LIVE" : "Orders · SHADOW")
            .toolbarBackground(mode == "live" ? Color.tdLive.opacity(0.18) : Color.clear, for: .navigationBar)
            .toolbarBackground(mode == "live" ? .visible : .automatic, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Limits", systemImage: "slider.horizontal.3") { showLimits = true }
                }
            }
            .safeAreaInset(edge: .bottom) { stopAllBar }
            .navigationDestination(for: Int.self) { LiveSessionView(sessionID: $0) }
            .sheet(isPresented: $showLimits) { LimitsView { showLimits = false; Task { await load() } } }
            .sheet(item: Binding(get: { legSheet.map(IdentifiedLeg.init) }, set: { legSheet = $0?.leg })) { il in
                LegActionSheet(leg: il.leg, mode: mode) { kind, trigger in
                    legSheet = nil
                    Task { await command(kind, il.leg, trigger: trigger) }
                }
            }
            .confirmationDialog("Stop all trading?", isPresented: $confirmStopAll, titleVisibility: .visible) {
                Button("Stop all", role: .destructive) { Task { await stopAll() } }
            } message: {
                Text(runnerDown
                     ? "The live runner is not answering: this only queues. Your stops at Zerodha still protect you; close in Kite if needed."
                     : "Disarms every session, exits every LIVE and SHADOW leg (each stop order at Zerodha becomes the exit; at the next open if the market is closed), and blocks new entries for today.")
            }
            .refreshable { await load() }
            .task(id: mode) {
                desk = nil; checklist = nil; loadError = nil          // never show one mode's legs under the other
                while !Task.isCancelled {
                    await load()
                    await pollPending()
                    try? await Task.sleep(for: .seconds(pending.values.contains { $0.status == "queued" } ? 1 : 5))
                }
            }
        }
    }

    // MARK: Stop all (pinned) and failures

    private var stopAllBar: some View {
        Button(role: .destructive) { confirmStopAll = true } label: {
            HStack(spacing: 10) {
                if stopping { ProgressView().tint(.white) } else { Image(systemName: "stop.circle.fill").font(.title2) }
                Text("Stop all").font(.headline)
            }
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 56)
            .background(Color.tdBad, in: .rect(cornerRadius: 16))
        }
        .buttonStyle(.plain)
        .disabled(stopping)
        .padding(.horizontal).padding(.vertical, 8)
        .background(.bar)
        .accessibilityHint("Asks to confirm, then exits every live and shadow leg and blocks new entries today")
    }

    private var runnerDownCard: some View {
        Section {
            Label("The live runner is not answering", systemImage: "exclamationmark.octagon.fill")
                .font(.headline).foregroundStyle(Color.tdBad)
            Text("Orders can't be sent or stopped from this app right now. Your stops at Zerodha keep working. Close positions in Kite if needed, or use Zerodha Call & Trade (number in the Kite app › Support).")
                .font(.footnote)
            kiteButton
        }
    }

    private func stopAllFailedCard(_ o: StopAllOutcome) -> some View {
        Section {
            Label("Stop all not confirmed", systemImage: "exclamationmark.octagon.fill").font(.headline).foregroundStyle(Color.tdBad)
            Text(o.text).font(.footnote)
            Button { Task { await stopAll(retry: true) } } label: {
                Label("Try Stop all again", systemImage: "arrow.clockwise").frame(minHeight: 44)
            }
            .tint(Color.tdBad)
            kiteButton
        }
    }

    private var kiteButton: some View {
        Link(destination: URL(string: "https://kite.zerodha.com/positions")!) {
            Label("Open Kite positions", systemImage: "arrow.up.forward.app").frame(minHeight: 44)
        }
    }

    // MARK: checklist

    private func checklistSection(_ c: JSONValue) -> some View {
        Section {
            ForEach(Array((c["items"]?.array ?? []).enumerated()), id: \.offset) { _, i in
                let ok = i["ok"]?.bool == true
                Label {
                    VStack(alignment: .leading) {
                        Text(i["name"]?.string ?? "").font(.subheadline)
                        Text(i["detail"]?.string ?? "").font(.caption).foregroundStyle(.secondary)
                    }
                } icon: {
                    Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle").foregroundStyle(ok ? Color.tdGood : Color.tdBad)
                }
                .accessibilityElement(children: .combine)
                .accessibilityValue(ok ? "OK" : "Not OK")
            }
        } header: {
            Text(c["ready"]?.bool == true ? "Ready for live" : "Before live orders")
        }
    }

    // MARK: desk

    @ViewBuilder private func deskSections(_ d: JSONValue) -> some View {
        let label = mode == "live" ? "LIVE" : "SHADOW"
        Section {
            ViewThatFits(in: .horizontal) {
                HStack { deskPills }
                VStack(alignment: .leading, spacing: 6) { deskPills }
            }
            if let why = d["why"]?.string, !why.isEmpty { Text(why).font(.footnote).foregroundStyle(Color.tdWarn) }
            if let lock = d["locked"]?.string { Label("Locked today: \(lock)", systemImage: "lock.fill").font(.footnote).foregroundStyle(Color.tdBad) }
            Group {
                if let mtm = d["mtm"]?.number {
                    PnlLine(label: "\(label) today, after estimated charges", value: mtm, big: true, mode: label.capitalized)
                } else if openLegs(d) > 0 {
                    LabeledContent("Today", value: "unknown (no price)").font(.subheadline)
                }
                riskLine(d)
            }
            .opacity(stale ? 0.5 : 1)
            if let at = updatedAt {
                Text("as of \(Fmt.time(at)) IST").font(.caption2).foregroundStyle(.secondary)
            }
        }
        let legs = (d["legs"]?.array ?? []).filter { ($0["mode"]?.string ?? mode) == mode }
        Section("Legs") {
            if legs.isEmpty { Text("No legs today.").foregroundStyle(.secondary) }
            ForEach(Array(legs.enumerated()), id: \.offset) { _, lg in
                let lid = Int(lg["id"]?.number ?? -1)
                Button { if lid > 0, ["open", "entering"].contains(lg["status"]?.string ?? "") { legSheet = lg } } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        DeskLegRow(lg: lg)
                        if let p = pending.values.first(where: { $0.legID == lid }) { pendingLine(p) }
                    }
                }
                .buttonStyle(.plain)
                .opacity(stale ? 0.5 : 1)
                .accessibilityAction(named: "Exit or move stop") { if lid > 0 { legSheet = lg } }
            }
        }
        let events = (d["events"]?.array ?? [])
        if !events.isEmpty {
            Section("What happened") {
                ForEach(Array(events.prefix(30).enumerated()), id: \.offset) { _, e in
                    Label {
                        VStack(alignment: .leading) {
                            Text(e["text"]?.string ?? "").font(.caption)
                            if let at = e["at"]?.number {
                                Text(Fmt.time(Date(timeIntervalSince1970: at))).font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    } icon: {
                        Image(systemName: Level.icon(e["level"]?.string ?? "info")).foregroundStyle(Level.color(e["level"]?.string ?? "info"))
                    }
                }
            }
        }
    }

    @ViewBuilder private var deskPills: some View {
        StatusPill(text: mode == "live" ? "LIVE" : "SHADOW", systemImage: mode == "live" ? "bolt.fill" : "eye", color: modeColor)
        if stale {
            StatusPill(text: updatedAt.map { "Stale · \(Int(Date.now.timeIntervalSince($0)))s" } ?? "Stale",
                       systemImage: "clock.badge.exclamationmark", color: .tdWarn)
        }
        if desk?["closing_only"]?.bool == true || (desk?["why"]?.string ?? "").contains("closing only") {
            StatusPill(text: "Closing only", systemImage: "arrow.down.right.circle", color: .tdWarn)
        }
    }

    private func openLegs(_ d: JSONValue) -> Int {
        (d["legs"]?.array ?? []).filter { ["open", "entering", "exiting"].contains($0["status"]?.string ?? "") }.count
    }

    /// If every stop fills, and how much of the day-loss limit is left.
    @ViewBuilder private func riskLine(_ d: JSONValue) -> some View {
        let atStops = (d["legs"]?.array ?? []).compactMap { lg -> Double? in
            guard ["open", "entering"].contains(lg["status"]?.string ?? ""), let sl = lg["sl_trigger"]?.number,
                  let entry = lg["entry_avg"]?.number else { return nil }
            let q = lg["open_qty"]?.number ?? lg["qty"]?.number ?? 0
            return (lg["side"]?.string == "SELL" ? entry - sl : sl - entry) * q
        }
        if let limit = d["limits"]?["daily_loss"]?.number {
            let used = min(0, d["mtm"]?.number ?? 0)
            Text("\(atStops.isEmpty ? "" : "If all stops fill \(Fmt.rupees(atStops.reduce(0, +), signed: true)) · ")day loss left \(Fmt.rupees(max(0, limit + used))) of \(Fmt.rupees(limit))")
                .font(.caption).foregroundStyle(.secondary).monospacedDigit()
        }
    }

    private func pendingLine(_ p: PendingCommand) -> some View {
        let failed = p.status == "failed"
        let text = p.status == "queued" ? "\(p.what) queued…" : (p.result.isEmpty ? "\(p.what) done" : p.result)
        return Label(text, systemImage: failed ? "xmark.octagon.fill" : p.status == "queued" ? "hourglass" : "checkmark.circle")
            .font(.caption).foregroundStyle(failed ? Color.tdBad : p.status == "queued" ? Color.tdWarn : .secondary)
    }

    private var sessionsSection: some View {
        Section {
            let mine = sessions.filter { $0["mode"]?.string == mode }
            if mine.isEmpty {
                Text(mode == "live" ? "No live sessions. A shadow session that ran 5 trading days can move to live."
                                    : "No shadow sessions. A paper session that met its gate can move to shadow (Trade › session › Results).")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            ForEach(Array(mine.enumerated()), id: \.offset) { _, s in
                NavigationLink(value: Int(s["id"]?.number ?? 0)) {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(s["name"]?.string ?? "").font(.headline).lineLimit(2)
                            Spacer()
                            if s["mode"]?.string == "live" {
                                StatusPill(text: s["armed_today"]?.bool == true ? "ARMED" : "Not armed",
                                           systemImage: s["armed_today"]?.bool == true ? "bolt.fill" : "bolt.slash",
                                           color: s["armed_today"]?.bool == true ? .tdLive : .secondary)
                            }
                        }
                        Text("\(s["underlying"]?.string ?? "") · \(humanize(s["status"]?.string ?? "")) · \(Int(s["open_legs"]?.number ?? 0)) open legs")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        } header: { Text(mode == "live" ? "Live sessions" : "Shadow sessions") }
    }

    // MARK: network

    private func load() async {
        let asked = mode
        do {
            async let dq = model.api.raw("GET", "/v1/live/desk?mode=\(asked)")
            async let sq = model.api.raw("GET", "/v1/live/sessions")
            let d = try await dq
            let s = try await sq
            guard asked == mode, d["mode"]?.string == asked else { return }   // a reply for the other mode is dropped
            desk = d
            sessions = s.array ?? []
            if asked == "live" { checklist = try? await model.api.raw("GET", "/v1/live/checklist") }
            loadError = nil; lastFailed = false; loadedAt = .now
        } catch {
            lastFailed = true
            loadError = "Couldn't load orders: \(error.localizedDescription). Figures below may be old."
        }
    }

    private func stopAll(retry: Bool = false) async {
        stopping = true; defer { stopping = false }
        let id = (retry ? stopAllID : nil) ?? "kill-" + UUID().uuidString.prefix(18).lowercased()
        stopAllID = id
        do {
            let r = try await model.api.raw("POST", "/v1/live/stop-all", body: JSONValue.object(["id": .string(id)]))
            let problems = (r["problems"]?.array ?? []).compactMap(\.string)
            let ok = r["runner_ok"]?.bool == true && problems.isEmpty
            stopAllOutcome = StopAllOutcome(ok: ok, text: r["note"]?.string ?? "")
            for c in r["commands"]?.array ?? [] {
                if let cid = c["id"]?.string { pending[cid] = PendingCommand(legID: -1, what: "Stop all (\(c["mode"]?.string ?? ""))") }
            }
            await load()
        } catch {
            stopAllOutcome = StopAllOutcome(ok: false, text: "Stop all did not reach the server (\(error.localizedDescription)). Try again, or close positions in Kite now.")
        }
    }

    private func command(_ kind: String, _ lg: JSONValue, trigger: Double? = nil) async {
        guard let lid = lg["id"]?.number, lid > 0, (lg["mode"]?.string ?? mode) == mode else {
            loadError = "That leg is not on this desk any more. Refresh and try again."; return
        }
        let cid = "cmd-" + UUID().uuidString.prefix(20).lowercased()
        var body: [String: JSONValue] = ["id": .string(cid), "mode": .string(lg["mode"]?.string ?? mode),
                                         "kind": .string(kind), "leg_id": .number(lid)]
        if let trigger { body["trigger"] = .number(trigger) }
        pending[cid] = PendingCommand(legID: Int(lid), what: kind == "exit_leg" ? "Exit" : "Stop move")
        do {
            let r = try await model.api.raw("POST", "/v1/live/commands", body: JSONValue.object(body))
            if r["runner_ok"]?.bool == false {
                pending[cid]?.result = r["note"]?.string ?? "Queued, but the live runner is not answering."
            }
        } catch {
            pending[cid]?.status = "failed"
            pending[cid]?.result = "Not sent: \(error.localizedDescription)"
        }
    }

    /// What the runner actually did with each command (its own words), with a haptic when it lands.
    private func pollPending() async {
        for (cid, p) in pending where p.status == "queued" {
            guard let r = try? await model.api.raw("GET", "/v1/live/commands/\(cid)") else { continue }
            let st = r["status"]?.string ?? "queued"
            if st != "queued" {
                pending[cid]?.status = st
                pending[cid]?.result = r["result"]?.string ?? ""
                if p.legID == -1 {
                    stopAllOutcome = StopAllOutcome(ok: st == "done", text: r["result"]?.string ?? "")
                }
                UINotificationFeedbackGenerator().notificationOccurred(st == "done" ? .success : .error)
            }
        }
    }
}

private struct IdentifiedLeg: Identifiable {
    let leg: JSONValue
    var id: Int { Int(leg["id"]?.number ?? 0) }
}

extension ISO8601DateFormatter {
    static let flexible: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
}

/// Exit or move the stop of one leg, with everything needed to decide: side, price now, stop, and ₹ at the stop.
/// A new stop must be tighter and on the safe side of the price; one past the price says plainly it exits now.
struct LegActionSheet: View {
    let leg: JSONValue
    let mode: String
    var send: (String, Double?) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var trigger: Double = 0
    @State private var confirmExitNow = false

    private var short: Bool { leg["side"]?.string == "SELL" }
    private var entry: Double { leg["entry_avg"]?.number ?? 0 }
    private var qty: Double { leg["open_qty"]?.number ?? leg["qty"]?.number ?? 0 }
    private var ltp: Double? { leg["ltp"]?.number }
    private var stop: Double? { leg["sl_trigger"]?.number }
    private func loss(_ sl: Double) -> Double { (short ? entry - sl : sl - entry) * qty }
    private var tighter: Bool { guard let stop else { return false }; return short ? trigger < stop : trigger > stop }
    private var pastPrice: Bool { guard let ltp else { return false }; return short ? trigger <= ltp : trigger >= ltp }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("\(short ? "Sell" : "Buy") \(leg["symbol"]?.string ?? "")").font(.headline)
                    LabeledContent("Quantity", value: "\(Int(qty))")
                    LabeledContent("Entry", value: Fmt.rupees(entry, decimals: 2))
                    LabeledContent("Now", value: ltp.map { Fmt.rupees($0, decimals: 2) } ?? "unknown")
                    if let stop {
                        LabeledContent("Stop at Zerodha") {
                            Text("\(Fmt.rupees(stop, decimals: 2)) (\(Fmt.rupees(loss(stop), signed: true)))")
                        }
                    }
                } header: { Text(mode == "live" ? "LIVE leg" : "SHADOW leg") }
                .monospacedDigit()
                if stop != nil {
                    Section {
                        HStack {
                            Text("New stop")
                            Spacer()
                            TextField("Trigger", value: $trigger, format: .number.precision(.fractionLength(2)))
                                .keyboardType(.decimalPad).multilineTextAlignment(.trailing).frame(width: 110)
                            Stepper("", value: $trigger, step: 0.05).labelsHidden()
                        }
                        .frame(minHeight: 44)
                        LabeledContent("At the new stop", value: Fmt.rupees(loss(trigger), signed: true)).monospacedDigit()
                        if !tighter {
                            Label(short ? "A tighter stop on a sold leg is lower than \(Fmt.rupees(stop ?? 0, decimals: 2))."
                                        : "A tighter stop on a bought leg is higher than \(Fmt.rupees(stop ?? 0, decimals: 2)).",
                                  systemImage: "info.circle").font(.caption).foregroundStyle(.secondary)
                        }
                        if tighter && pastPrice {
                            Button(role: .destructive) { confirmExitNow = true } label: {
                                Label("This exits the leg now", systemImage: "exclamationmark.triangle.fill").frame(minHeight: 44)
                            }
                        } else {
                            Button { send("tighten_stop", trigger) } label: { Text("Move stop").frame(minHeight: 44) }
                                .disabled(!tighter || trigger <= 0)
                        }
                    } header: { Text("Move the stop (tighter only)") }
                }
                Section {
                    Button(role: .destructive) { send("exit_leg", nil) } label: {
                        Label("Exit this leg now", systemImage: "xmark.circle").frame(minHeight: 44)
                    }
                } footer: { Text("Its stop order at Zerodha becomes a limit exit at the touch (at the next open if the market is closed).") }
            }
            .navigationTitle("Leg")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
            .confirmationDialog("The new stop is past the price: the leg exits now.", isPresented: $confirmExitNow, titleVisibility: .visible) {
                Button("Exit now", role: .destructive) { send("tighten_stop", trigger) }
            }
            .onAppear { trigger = stop ?? 0 }
        }
        .presentationDetents([.medium, .large])
    }
}

/// A desk leg: side, lots and quantity, entry / now, and whether a stop is really working at Zerodha.
struct DeskLegRow: View {
    let lg: JSONValue
    var body: some View {
        let short = lg["side"]?.string == "SELL"
        let entry = lg["entry_avg"]?.number
        let ltp = lg["ltp"]?.number
        let qty = lg["open_qty"]?.number ?? lg["qty"]?.number ?? 0
        let status = lg["status"]?.string ?? ""
        let live = ["open", "entering", "exiting"].contains(status)
        let sym = lg["symbol"]?.string ?? ""
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(short ? "SELL" : "BUY").font(.caption2.weight(.bold))
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background((short ? Color.tdBad : Color.tdGood).opacity(0.15), in: .capsule)
                Text(sym.hasSuffix("CE") ? "CE" : sym.hasSuffix("PE") ? "PE" : "").font(.caption2.weight(.bold))
                Text(sym).font(.subheadline.weight(.semibold)).lineLimit(1)
                Spacer()
                Text(humanize(status)).font(.caption.weight(.semibold)).foregroundStyle(status == "open" ? Color.primary : .secondary)
            }
            Text("\(Int(qty)) qty · in \(entry.map { Fmt.rupees($0, decimals: 2) } ?? "—") · now \(ltp.map { Fmt.rupees($0, decimals: 2) } ?? "—")")
                .font(.caption).foregroundStyle(.secondary).monospacedDigit()
            if live {
                if let sl = lg["sl_trigger"]?.number {
                    let loss = entry.map { (short ? $0 - sl : sl - $0) * qty }
                    if lg["stop_working"]?.bool == true {
                        Label("Stop at Zerodha \(sl.formatted(.number.precision(.fractionLength(2))))\(loss.map { " · at stop \(Fmt.rupees($0, signed: true))" } ?? "")",
                              systemImage: "shield.fill")
                            .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                    } else if status == "exiting" {
                        Label("Exiting: its stop order became the exit", systemImage: "arrow.down.right.circle").font(.caption).foregroundStyle(Color.tdWarn)
                    } else {
                        Label("Stop \(sl.formatted(.number.precision(.fractionLength(2)))) not working at Zerodha yet", systemImage: "exclamationmark.shield")
                            .font(.caption).foregroundStyle(Color.tdWarn)
                    }
                } else if lg["hedge"]?.bool == true {
                    Label("Hedge: caps an open short leg (no stop by design)", systemImage: "shield.lefthalf.filled").font(.caption).foregroundStyle(.secondary)
                } else {
                    Label("No stop at Zerodha", systemImage: "exclamationmark.octagon.fill").font(.caption).foregroundStyle(Color.tdBad)
                }
            }
            if let r = lg["reason"]?.string, !r.isEmpty { Text(humanize(r)).font(.caption2).foregroundStyle(.secondary) }
        }
        .accessibilityElement(children: .combine)
    }
}
