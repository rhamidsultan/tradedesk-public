import Charts
import SwiftUI

/// One paper session: P&L, open legs with their distance to the stop, actions (each takes effect at the next
/// minute), fills, results (cycles, calendar, paper vs backtest, gate) and scrubbing back through a booked day.
struct PaperSessionView: View {
    @Environment(AppModel.self) private var model
    let sessionID: Int
    @State private var d: JSONValue?
    @State private var results: JSONValue?
    @State private var error: String?
    @State private var note: String?
    @State private var section = 0
    @State private var confirm: Action?
    @State private var busy = false
    @State private var excludeCycle: Int?
    @State private var excludeWhy = ""
    @State private var refreshed: Date?
    @State private var shadowStarted = false

    enum Action: String, Identifiable {
        case pause, resume, squareOff = "square-off", stop, reset, copy
        var id: String { rawValue }
        var title: String {
            switch self {
            case .pause: "Pause new entries?"
            case .resume: "Resume entries?"
            case .squareOff: "Square off every open leg?"
            case .stop: "Stop this session?"
            case .reset: "Reset this session?"
            case .copy: "Copy this session?"
            }
        }
        /// What happens and when, built from the session: its name, open legs, P&L and the server's next minute.
        func message(_ d: JSONValue?) -> String {
            let name = d?["name"]?.string ?? "this session"
            let legs = Int(d?["open_legs"]?.number ?? 0)
            let when = d?["next_effect"]?.string ?? "at the next minute"
            let pnl = d?["state"]?["day_pnl"]?.number.map { " Now \(Fmt.rupees($0, signed: true)) PAPER today." } ?? ""
            let cycles = Int(d?["state"]?["gate"]?["cycles"]?.number ?? 0)
            switch self {
            case .pause: return "\(name): no new entries from \(when). \(legs) open leg\(legs == 1 ? "" : "s") keep their stops and exits."
            case .resume: return "\(name): entries again from \(when)."
            case .squareOff: return "\(legs) open leg\(legs == 1 ? "" : "s") of \(name) exit \(when).\(pnl) The session keeps running."
            case .stop: return "\(name): \(legs) open leg\(legs == 1 ? "" : "s") exit \(when), then the session ends after that day's close.\(pnl) It can't be restarted, only copied."
            case .reset: return "Clears \(cycles) cycle\(cycles == 1 ? "" : "s"), the day results and the gate count of \(name), and starts again \(when). Can't be undone."
            case .copy: return "A new session with the same frozen settings as \(name), starting \(when)."
            }
        }
        var destructive: Bool { [.squareOff, .stop, .reset].contains(self) }
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
                header(d)
                if !(d["kill"]?.array ?? []).isEmpty {
                    Section {
                        Label("A kill rule fired: return the strategy to research and change it. This session can't be reset or copied.",
                              systemImage: "exclamationmark.octagon.fill").font(.footnote).foregroundStyle(Color.tdBad)
                    }
                }
                Picker("Show", selection: $section) {
                    Text("Now").tag(0); Text("Results").tag(1); Text("Replay a day").tag(2)
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
                switch section {
                case 0: nowSections(d["state"])
                case 1: resultSections
                default: ScrubSection(sessionID: sessionID, days: (results?["kept_days"]?.array ?? d["state"]?["kept_days"]?.array ?? []).compactMap(\.string))
                }
            } else if error == nil {
                ProgressView()
            }
        }
        .safeAreaInset(edge: .bottom) { if let d { actionBar(d) } }
        .navigationTitle(d?["name"]?.string ?? "Paper session")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            let killed = !(d?["kill"]?.array ?? []).isEmpty
            if d?["status"]?.string != "stopped" {
                Menu {
                    if d?["status"]?.string != "stopping" {
                        Button("Stop session", systemImage: "stop.fill", role: .destructive) { confirm = .stop }
                    }
                    if !killed {
                        Button("Reset", systemImage: "arrow.counterclockwise", role: .destructive) { confirm = .reset }
                            .disabled((d?["open_legs"]?.number ?? 0) > 0)
                        Button("Copy", systemImage: "doc.on.doc") { confirm = .copy }
                    }
                } label: { Image(systemName: "ellipsis.circle").accessibilityLabel("More actions") }
            } else if !killed {
                Button("Copy", systemImage: "doc.on.doc") { confirm = .copy }
            }
        }
        .confirmationDialog(confirm?.title ?? "", isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }),
                            titleVisibility: .visible, presenting: confirm) { a in
            Button(a.rawValue == "square-off" ? "Square off" : a.rawValue.capitalized, role: a.destructive ? .destructive : nil) {
                Task { await act(a) }
            }
        } message: { a in Text(a.message(d)) }
        .alert("Leave this cycle out of the figures?", isPresented: Binding(get: { excludeCycle != nil }, set: { if !$0 { excludeCycle = nil } })) {
            TextField("Why (required)", text: $excludeWhy)
            Button("Leave out") { if let c = excludeCycle { Task { await exclude(c, true) } } }
                .disabled(excludeWhy.trimmingCharacters(in: .whitespaces).isEmpty)
            Button("Cancel", role: .cancel) {}
        } message: { Text("It stays listed and marked, and still counts towards the gate.") }
        .refreshable { await load() }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(for: .seconds(d?["state"]?["live"]?.bool == true ? 20 : 120))
            }
        }
    }

    // MARK: header and actions

    private func header(_ d: JSONValue) -> some View {
        let st = d["state"]
        return Section {
            HStack {
                StatusPill(text: "PAPER", systemImage: "doc.text", color: .tdPaper)
                if PaperClock.isStale(asOf: st?["as_of"]?.string, live: st?["live"]?.bool == true) {
                    StatusPill(text: "Stale · as of \(st?["as_of"]?.string ?? "—")", systemImage: "clock.badge.exclamationmark", color: .tdWarn)
                }
                Spacer()
                Text(statusText(d)).font(.subheadline.weight(.semibold))
            }
            if let refreshed { Text("Refreshed \(Fmt.time(refreshed))").font(.caption2).foregroundStyle(.secondary) }
            if let day = st?["day_pnl"]?.number {
                PnlLine(label: st?["live"]?.bool == true ? "Today\(st?["as_of"]?.string.map { " · as of \($0)" } ?? "")"
                                                          : "Last day · \(Fmt.marketDay(st?["day_date"]?.string))",
                        value: day, big: true)
            }
            PnlLine(label: "Total since \(Fmt.marketDay(d["first_day"]?.string))", value: st?["cum_net"]?.number ?? 0)
            if let m = st?["margin"], m != .null {
                if let f = m["final"]?.number {
                    LabeledContent("Margin (Zerodha basket)", value: "\(Fmt.rupees(f)) · \(m["at"]?.string ?? "")").font(.caption)
                } else if let why = m["unknown"]?.string {
                    LabeledContent("Margin", value: why).font(.caption)
                }
            }
            ForEach((st?["kill"]?.array ?? []).compactMap(\.string), id: \.self) { k in
                Label(k, systemImage: "exclamationmark.octagon.fill").foregroundStyle(Color.tdBad).font(.subheadline)
            }
            if let e = st?["error"]?.string {
                Label("The server hit a problem replaying this session: \(e)", systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(Color.tdWarn)
            }
        }
        .monospacedDigit()
    }

    private func statusText(_ d: JSONValue) -> String {
        switch d["status"]?.string {
        case "running": "Running"
        case "paused": (d["kill"]?.array?.isEmpty == false) ? "Paused by a kill rule" : "Paused"
        case "stopping": "Stopping after the close"
        default: "Ended"
        }
    }

    /// Pause/Resume and Square off within thumb reach; Stop, Reset and Copy are in the ⋯ menu.
    @ViewBuilder private func actionBar(_ d: JSONValue) -> some View {
        let status = d["status"]?.string ?? ""
        if status != "stopped" {
            VStack(spacing: 4) {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) { actionButtons(d, status) }
                    VStack(spacing: 8) { actionButtons(d, status) }
                }
                .disabled(busy)
                Text("PAPER · takes effect \(d["next_effect"]?.string ?? "at the next minute") · no real orders")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            .padding(.horizontal).padding(.top, 8)
            .background(.bar)
        }
    }

    @ViewBuilder private func actionButtons(_ d: JSONValue, _ status: String) -> some View {
        if status == "running" {
            Button { confirm = .pause } label: { Label("Pause", systemImage: "pause.fill").frame(maxWidth: .infinity, minHeight: 50) }
                .buttonStyle(.bordered)
        } else if status == "paused" {
            Button { confirm = .resume } label: { Label("Resume", systemImage: "play.fill").frame(maxWidth: .infinity, minHeight: 50) }
                .buttonStyle(.bordered)
                .disabled(d["kill"]?.array?.isEmpty == false)
        }
        Button(role: .destructive) { confirm = .squareOff } label: {
            Label("Square off", systemImage: "xmark.circle").frame(maxWidth: .infinity, minHeight: 50)
        }
        .buttonStyle(.bordered).tint(Color.tdBad)
        .disabled((d["open_legs"]?.number ?? 0) == 0)
    }

    // MARK: now

    @ViewBuilder private func nowSections(_ st: JSONValue?) -> some View {
        let pos = st?["positions"]?.array ?? []
        Section("Open legs") {
            if pos.isEmpty { Text("No open legs.").foregroundStyle(.secondary) }
            ForEach(Array(pos.enumerated()), id: \.offset) { _, p in LegRow(p: p, spot: st?["spot"]?.number) }
        }
        if !pos.isEmpty {
            Section {
                PayoffChart(legs: pos.compactMap(PayoffLeg.init(json:)))
                    .frame(height: 200)
            } header: { Text("Payoff at expiry") } footer: {
                Text("P&L of the open legs if the index ends at each level on expiry, before charges.")
            }
        }
        let fills = (st?["fills_today"]?.array ?? []).reversed()
        Section("Fills today") {
            if fills.isEmpty { Text("No fills today.").foregroundStyle(.secondary) }
            ForEach(Array(fills.enumerated()), id: \.offset) { _, f in FillRow(f: f) }
        }
    }

    // MARK: results

    @ViewBuilder private var resultSections: some View {
        if let r = results {
            let s = r["summary"]
            Section {
                PnlLine(label: "Net after all charges", value: s?["net"]?.number ?? 0, big: true)
                if (s?["excluded"]?.number ?? 0) > 0 {
                    PnlLine(label: "Including \(Int(s?["excluded"]?.number ?? 0)) left-out cycles", value: s?["net_all"]?.number ?? 0)
                }
                LabeledContent("Cycles", value: "\(Int(s?["cycles"]?.number ?? 0))")
                LabeledContent("Win rate", value: s?["win_rate"]?.number.map { "\($0.formatted())%" } ?? "—")
                LabeledContent("Average win / loss", value: "\(money(s?["avg_win"]?.number)) / \(money(s?["avg_loss"]?.number))")
                LabeledContent("Expectancy a cycle", value: money(s?["expectancy"]?.number))
                LabeledContent("Max drawdown", value: money(s?["max_drawdown"]?.number.map { -$0 }))
                LabeledContent("Charges paid", value: s?["charges"]?.number.map { Fmt.rupees($0) } ?? "—")
            } header: { Text("Results") }
            .monospacedDigit()
            if let g = r["gate"], g != .null { gateSection(g) }
            let cal = r["calendar"]?.array ?? []
            if !cal.isEmpty {
                Section("Days") {
                    Chart(Array(cal.enumerated()), id: \.offset) { _, x in
                        BarMark(x: .value("Day", x["date"]?.string ?? ""), y: .value("Net", x["net"]?.number ?? 0))
                            .foregroundStyle((x["net"]?.number ?? 0) >= 0 ? Color.tdGood : Color.tdBad)
                    }
                    .chartXAxis(.hidden)
                    .frame(height: 140)
                    ForEach(Array(cal.reversed().prefix(30).enumerated()), id: \.offset) { _, x in
                        LabeledContent(Fmt.marketDay(x["date"]?.string)) {
                            Text(Fmt.rupees(x["net"]?.number ?? 0, signed: true))
                                .foregroundStyle((x["net"]?.number ?? 0) > 0 ? Color.tdGood : (x["net"]?.number ?? 0) < 0 ? Color.tdBad : .secondary)
                        }
                        .monospacedDigit()
                    }
                }
            }
            let cmp = r["vs_backtest"]?.array ?? []
            if !cmp.isEmpty {
                Section {
                    ForEach(Array(cmp.reversed().prefix(20).enumerated()), id: \.offset) { _, x in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(Fmt.marketDay(x["date"]?.string)).font(.subheadline)
                            Text("Paper \(Fmt.rupees(x["paper"]?.number ?? 0, signed: true)) · backtest \(Fmt.rupees(x["backtest"]?.number ?? 0, signed: true)) · difference \(Fmt.rupees(x["diff"]?.number ?? 0, signed: true))")
                                .font(.caption).monospacedDigit()
                                .foregroundStyle(abs(x["diff"]?.number ?? 0) > 1 ? Color.tdWarn : .secondary)
                        }
                    }
                } header: { Text("Paper vs backtest") } footer: {
                    Text("The same strategy backtested on the same recorded prices without your actions. Differences come from pauses, square-offs, a mid-day start, or prices that arrived late or not at all.")
                }
            }
            let cycles = r["cycles"]?.array ?? []
            Section("Cycles") {
                if cycles.isEmpty { Text("No closed cycles yet.").foregroundStyle(.secondary) }
                ForEach(Array(cycles.enumerated()), id: \.offset) { _, c in
                    CycleRow(c: c)
                        .swipeActions {
                            if c["excluded"]?.bool == true {
                                Button("Include") { Task { await exclude(Int(c["id"]?.number ?? 0), false) } }.tint(Color.tdPaper)
                            } else {
                                Button("Leave out") { excludeWhy = ""; excludeCycle = Int(c["id"]?.number ?? 0) }.tint(Color.tdWarn)
                            }
                        }
                }
            }
        } else {
            ProgressView()
        }
    }

    private func gateSection(_ g: JSONValue) -> some View {
        Section {
            gaugeRow("Cycles", g["cycles"]?.number ?? 0, g["cycles_needed"]?.number ?? 20)
            gaugeRow("Weeks", g["weeks"]?.number ?? 0, g["weeks_needed"]?.number ?? 8)
            gaugeRow("Stop events", g["stop_events"]?.number ?? 0, g["stop_events_needed"]?.number ?? 15)
            if let m = g["median_slip"]?.number {
                LabeledContent("Median stop slippage", value: "₹\(m.formatted()) a unit")
            }
            Label(g["met"]?.bool == true ? "Gate met: ready for the next stage." : "Gate not met yet.",
                  systemImage: g["met"]?.bool == true ? "checkmark.seal.fill" : "hourglass")
                .foregroundStyle(g["met"]?.bool == true ? Color.tdGood : .secondary)
            if g["met"]?.bool == true {
                if shadowStarted {
                    Button { model.ordersMode = "shadow"; model.tab = .orders } label: {
                        Label("Open in Orders › Shadow", systemImage: "arrow.right.circle").frame(minHeight: 44)
                    }
                } else {
                    Button { Task { await startShadow() } } label: {
                        HStack { Label("Start shadow (real prices, nothing sent)", systemImage: "eye"); if busy { ProgressView() } }
                            .frame(minHeight: 44)
                    }
                    .disabled(busy)
                }
            }
        } header: { Text("Gate to the next stage") }
    }

    private func gaugeRow(_ label: String, _ v: Double, _ need: Double) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(label): \(v.formatted()) of \(need.formatted())").font(.subheadline).monospacedDigit()
            Gauge(value: min(v, need), in: 0...max(need, 1)) { EmptyView() }
                .gaugeStyle(.accessoryLinear).tint(v >= need ? Color.tdGood : Color.tdPaper)
        }
        .accessibilityElement(children: .combine)
    }

    private func money(_ v: Double?) -> String { v.map { Fmt.rupees($0, signed: true) } ?? "—" }

    // MARK: network

    private func load() async {
        do {
            d = try await model.api.raw("GET", "/v1/paper/sessions/\(sessionID)")
            refreshed = .now
            if section == 1 || results == nil {
                results = try await model.api.raw("GET", "/v1/paper/sessions/\(sessionID)/results")
            }
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func startShadow() async {
        busy = true; defer { busy = false }
        do {
            let r = try await model.api.raw("POST", "/v1/live/sessions",
                                            body: JSONValue.object(["mode": .string("shadow"), "paper_id": .number(Double(sessionID))]))
            note = r["note"]?.string ?? "Shadow session created."
            shadowStarted = true
        } catch { self.error = error.localizedDescription }
    }

    private func act(_ a: Action) async {
        busy = true; defer { busy = false }
        do {
            let r = try await model.api.raw("POST", "/v1/paper/sessions/\(sessionID)/\(a.rawValue)")
            note = r["note"]?.string ?? (a == .copy ? "Copied: find it in Trade." : nil)
            error = nil
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func exclude(_ id: Int, _ on: Bool) async {
        do {
            results = try await model.api.raw("POST", "/v1/paper/sessions/\(sessionID)/cycles/\(id)/exclude",
                                              body: JSONValue.object(["exclude": .bool(on), "why": .string(excludeWhy)]))
            error = nil
        } catch { self.error = error.localizedDescription }
    }
}

/// An open leg: P&L, the stop's distance in points and %, and the ₹ loss if the stop fills (the number that matters).
struct LegRow: View {
    let p: JSONValue
    var spot: Double? = nil

    var body: some View {
        let short = p["side"]?.string == "SELL"
        let ltp = p["ltp"]?.number ?? 0
        let qty = p["qty"]?.number ?? 0
        let entry = p["entry"]?.number ?? 0
        let lots = Int(p["lots"]?.number ?? 0)
        let pnl = p["pnl"]?.number ?? 0
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("\(short ? "Sell" : "Buy") \(Fmt.contract(p["symbol"]?.string ?? ""))").font(.subheadline.weight(.semibold))
                Spacer()
                Text(Fmt.rupees(pnl, signed: true)).foregroundStyle(pnl > 0 ? Color.tdGood : pnl < 0 ? Color.tdBad : .secondary)
            }
            Text("\(lots) lot\(lots == 1 ? "" : "s") · \(Int(qty)) qty · in \(Fmt.rupees(entry, decimals: 2)) · now \(Fmt.rupees(ltp, decimals: 2))")
                .font(.caption).foregroundStyle(.secondary)
            if let sl = p["sl"]?.number, ltp > 0 {
                let gap = short ? sl - ltp : ltp - sl
                let lossAtStop = (short ? entry - sl : sl - entry) * qty
                if gap <= 0 {
                    Label("Stop \(sl.formatted(.number.precision(.fractionLength(2)))) crossed · exits at the next minute",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.caption).foregroundStyle(Color.tdWarn)
                } else {
                    Label("Stop \(sl.formatted(.number.precision(.fractionLength(2)))) · \(gap.formatted(.number.precision(.fractionLength(2)))) pts away (\(Int((gap / ltp * 100).rounded()))%) · at stop \(Fmt.rupees(lossAtStop, signed: true))",
                          systemImage: gap / ltp < 0.2 ? "exclamationmark.triangle.fill" : "shield")
                        .font(.caption).foregroundStyle(gap / ltp < 0.2 ? Color.tdWarn : .secondary)
                }
            } else if let ssl = p["spot_sl"]?.number {
                let away = spot.map { " · \(Int(abs($0 - ssl).rounded())) pts away" } ?? ""
                Label("Index stop \(ssl.formatted(.number.precision(.fractionLength(0))))\(away) (held by the server)", systemImage: "shield")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .monospacedDigit()
        .accessibilityElement(children: .combine)
    }
}

struct FillRow: View {
    let f: JSONValue
    var body: some View {
        let why = f["reason"]?.string ?? ""
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text("\(f["side"]?.string == "SELL" ? "Sold" : "Bought") \(Int(f["qty"]?.number ?? 0)) \(Fmt.contract(f["symbol"]?.string ?? ""))")
                    .font(.subheadline)
                Spacer()
                Text(Fmt.rupees(f["price"]?.number ?? 0, decimals: 2)).monospacedDigit()
            }
            Text("\(Fmt.storeTime(f["ts"]?.string ?? "")) · \(humanize(why))\(f["stale"]?.bool == true ? " · late price" : "")")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

struct CycleRow: View {
    let c: JSONValue
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text("#\(Int(c["id"]?.number ?? 0)) · \(Fmt.storeTime(c["entry"]?.string ?? ""))").font(.subheadline)
                if c["excluded"]?.bool == true { StatusPill(text: "Left out", systemImage: "minus.circle", color: .tdWarn) }
                Spacer()
                Text(Fmt.rupees(c["net"]?.number ?? 0, signed: true))
                    .foregroundStyle((c["net"]?.number ?? 0) > 0 ? Color.tdGood : (c["net"]?.number ?? 0) < 0 ? Color.tdBad : .secondary).monospacedDigit()
            }
            Text("\(humanize(c["reason"]?.string ?? "")) · charges \(Fmt.rupees(c["charges"]?.number ?? 0))\(c["excluded_why"]?.string.map { " · \($0)" } ?? "")")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

/// Replay a booked day minute by minute: where the session stood at any minute.
struct ScrubSection: View {
    @Environment(AppModel.self) private var model
    let sessionID: Int
    let days: [String]
    @State private var day: String?
    @State private var minute = 0.0
    @State private var st: JSONValue?
    @State private var error: String?
    @State private var task: Task<Void, Never>?

    var body: some View {
        Section {
            if days.isEmpty {
                Text("Days appear here after the close once they are booked (the last 60 are kept).").foregroundStyle(.secondary)
            } else {
                Picker("Day", selection: $day) {
                    ForEach(days.reversed(), id: \.self) { Text(Fmt.marketDay($0)).tag(String?.some($0)) }
                }
                VStack(alignment: .leading) {
                    Text("At \(clock(Int(minute)))").font(.headline).monospacedDigit()
                    Slider(value: $minute, in: 0...374, step: 1) { Text("Minute") }
                        .accessibilityValue(clock(Int(minute)))
                }
                if let error { Text(error).font(.caption).foregroundStyle(Color.tdWarn) }
                if let st {
                    PnlLine(label: "Day P&L at \(st["as_of"]?.string ?? clock(Int(minute)))", value: st["day_pnl"]?.number ?? 0)
                    ForEach(Array((st["positions"]?.array ?? []).enumerated()), id: \.offset) { _, p in LegRow(p: p) }
                    if (st["positions"]?.array ?? []).isEmpty { Text("No open legs at this minute.").foregroundStyle(.secondary) }
                    ForEach(Array((st["fills_today"]?.array ?? []).reversed().prefix(10).enumerated()), id: \.offset) { _, f in FillRow(f: f) }
                }
            }
        } header: { Text("Replay a day") }
        .onAppear { if day == nil { day = days.last } }
        .onChange(of: day) { fetch() }
        .onChange(of: minute) { fetch() }
    }

    private func clock(_ m: Int) -> String {
        let t = 9 * 60 + 15 + m
        return String(format: "%02d:%02d", t / 60, t % 60)
    }

    private func fetch() {
        task?.cancel()
        guard let day else { return }
        let m = Int(minute)
        task = Task {
            try? await Task.sleep(for: .milliseconds(300))          // wait for the slider to settle
            if Task.isCancelled { return }
            do {
                st = try await model.api.raw("GET", "/v1/paper/sessions/\(sessionID)/scrub?day=\(day)&minute=\(m)")
                error = nil
            } catch { self.error = error.localizedDescription }
        }
    }
}
