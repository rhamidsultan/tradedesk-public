import SwiftUI

struct HomeView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true
    @State private var drafts: [JSONValue] = []
    @State private var paper: JSONValue?
    @State private var paperError = false

    var body: some View {
        @Bindable var model = model
        NavigationStack(path: $model.homePath) {
            ScrollView {
                GlassEffectContainer(spacing: 16) {
                    VStack(spacing: 16) {
                        if let c = model.status?.alerts.critical { criticalBanner(c) }   // always first
                        if model.can("market_data") { IndexStripView() }
                        if model.can("portfolio") { NetWorthCard() }
                        if let exp = model.installExpiry, exp.timeIntervalSinceNow < 2 * 86_400 { installCard(exp) }
                        marketCard
                        if model.can("paper", "use") || model.can("live", "use") { kiteCard }
                        alertsCard
                        if !drafts.isEmpty && model.can("research") { draftsCard }
                        if !model.latestRuns.isEmpty && model.can("backtests") { resultsCard }
                        if model.can("paper") { tradingCard }
                        if let e = model.lastError {
                            Label(e, systemImage: "wifi.exclamationmark").font(.footnote).foregroundStyle(Color.tdWarn)
                        }
                    }
                    .padding()
                }
            }
            .background(AppBackground())
            .refreshable {
                try? await model.refresh()
                await loadPaper()
            }
            .task {
                let all = model.can("research") ? ((try? await model.api.raw("GET", "/v1/strategies"))?.array ?? []) : []
                drafts = Array(all.filter { $0["has_draft"]?.bool == true }.prefix(3))
                while !Task.isCancelled {                     // PAPER figures every 30 s while Home is on screen
                    await loadPaper()
                    try? await Task.sleep(for: .seconds(30))
                }
            }
            .navigationTitle(greeting)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink(value: Route.alerts) {
                        Image(systemName: (model.status?.alerts.open ?? 0) > 0 ? "bell.badge" : "bell")
                    }
                    .accessibilityLabel("Alerts")
                }
            }
            .navigationDestination(for: Route.self) { route in
                switch route {
                case .alerts: AlertsView()
                case .broker: BrokerView()
                case .portfolio: PortfolioView()
                case .portfolioStock(let s): StockHistoryView(symbol: s, gold: false)
                case .us: USStocksView()
                default: EmptyView()
                }
            }
        }
    }

    private var greeting: String {
        var cal = Calendar.current
        cal.timeZone = showIST ? Fmt.ist : .current
        let h = cal.component(.hour, from: .now)
        return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening"
    }

    private func criticalBanner(_ c: StatusSnapshot.Alerts.Critical) -> some View {
        GlassCard(tint: .tdBad) {
            Label(c.title, systemImage: "exclamationmark.octagon.fill")
                .font(.headline).foregroundStyle(Color.tdBad)
            Text("Repeating every 2 minutes until you acknowledge it.").font(.footnote).foregroundStyle(.secondary)
            HStack {
                Button("Acknowledge", systemImage: "checkmark") {
                    if let a = model.openAlerts.first(where: { $0.id == c.id }) { Task { await model.ack(a) } }
                }
                .buttonStyle(.glassProminent)
                NavigationLink("Details", value: Route.alerts).buttonStyle(.glass)
            }
        }
    }

    private func installCard(_ exp: Date) -> some View {
        GlassCard(tint: .tdWarn) {
            Label("App stops in \(Provisioning.timeLeft(until: exp))", systemImage: "hourglass")
                .font(.headline).foregroundStyle(Color.tdWarn)
            Text("Ends \(Fmt.dayTime(exp, inIST: showIST)). Re-install from Xcode on the Mac tonight (same Wi-Fi, press Run).")
                .font(.footnote)
        }
    }

    private var marketCard: some View {
        let m = model.status?.market
        let (title, icon, color): (String, String, Color) = switch m?.state ?? "" {
        case "open": ("Market open", "sun.max.fill", .tdGood)
        case "pre_open": ("Pre-open", "sunrise.fill", .tdWarn)
        case "fo_only": ("F&O only (cash closed)", "sunset.fill", .tdWarn)
        case "closed": (m?.reason == "holiday" ? "Holiday" : m?.reason == "weekend" ? "Weekend" : "Market closed",
                        "moon.stars.fill", .secondary)
        default: ("Waiting for the server", "icloud.slash", .secondary)
        }
        return GlassCard {
            CardHeader(title: "Market", systemImage: "building.columns",
                       trailing: model.lastRefresh.map { (model.isStale ? "Stale · " : "Updated ") + Fmt.time($0, inIST: showIST) })
            Label(title, systemImage: icon).font(.title3.weight(.semibold)).foregroundStyle(color)
            if m?.state == "pre_open", let open = m?.open {
                Text("Opens \(Fmt.time(open, inIST: showIST))").font(.subheadline).foregroundStyle(.secondary)
            } else if let next = m?.nextOpen {
                Text("Next open \(Fmt.dayTime(next, inIST: showIST))").font(.subheadline).foregroundStyle(.secondary)
            } else if let close = m?.foClose {
                Text("F&O closes \(Fmt.time(close, inIST: showIST))").font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }

    private var kiteCard: some View {
        let k = model.status?.kite
        return GlassCard(tint: k?.connectedToday == true ? nil : .tdWarn) {
            CardHeader(title: "Zerodha", systemImage: "key", trailing: k?.clientId)
            if k?.setUp != true {
                Text("Add your Kite API key to log in each morning.").font(.subheadline)
                NavigationLink("Set up Kite", value: Route.broker).buttonStyle(.glass)
            } else if k?.connectedToday == true {
                Label("Logged in for today", systemImage: "checkmark.seal.fill").foregroundStyle(Color.tdGood)
            } else {
                Label("Login needed for today", systemImage: "exclamationmark.triangle.fill").foregroundStyle(Color.tdWarn)
                KiteLoginButton()
            }
        }
    }

    private var alertsCard: some View {
        GlassCard {
            CardHeader(title: "Alerts", systemImage: "bell", trailing: "\(model.openAlerts.count) open")
            if model.openAlerts.isEmpty {
                Text("Nothing needs you right now.").font(.subheadline).foregroundStyle(.secondary)
            } else {
                ForEach(model.openAlerts.prefix(3)) { a in
                    Label(a.title, systemImage: Level.icon(a.level)).foregroundStyle(Level.color(a.level)).font(.subheadline)
                }
                NavigationLink("See all", value: Route.alerts).font(.subheadline.weight(.semibold))
            }
        }
    }

    private var draftsCard: some View {
        GlassCard {
            CardHeader(title: "Continue editing", systemImage: "pencil.circle")
            ForEach(Array(drafts.enumerated()), id: \.offset) { _, d in
                Button { model.tab = .strategies } label: {
                    Label(d["name"]?.string ?? "", systemImage: "doc.badge.ellipsis").font(.subheadline)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var resultsCard: some View {
        GlassCard {
            CardHeader(title: "Latest backtests", systemImage: "chart.xyaxis.line")
            ForEach(model.latestRuns) { r in
                Button { model.tab = .strategies } label: { RunRow(run: r) }.buttonStyle(.plain)
            }
        }
    }

    private func loadPaper() async {
        guard model.can("paper") else { return }
        do {
            paper = try await model.api.raw("GET", "/v1/paper/overview")
            paperError = false
            await PaperActivity.shared.refresh(model.api)
        } catch { paperError = true }
    }

    private var paperCounts: String? {
        guard let p = paper else { return nil }
        var parts = ["\(Int(p["running"]?.number ?? 0)) running"]
        if let n = p["paused"]?.number, n > 0 { parts.append("\(Int(n)) paused") }
        if let a = p["as_of"]?.string { parts.append("as of \(a) IST") }
        return parts.joined(separator: " · ")
    }

    /// PAPER P&L, always apart from real money and marked as paper.
    private var tradingCard: some View {
        GlassCard(tint: .tdPaper) {
            CardHeader(title: "Paper trading", systemImage: "doc.text", trailing: paperCounts)
            if paperError {
                HStack {
                    Label("Couldn't load paper sessions", systemImage: "wifi.exclamationmark").foregroundStyle(Color.tdWarn)
                    Spacer()
                    Button("Retry") { Task { await loadPaper() } }
                }
                .font(.subheadline)
            }
            if let p = paper, (p["sessions"]?.number ?? 0) > 0 {
                if PaperClock.isStale(asOf: p["as_of"]?.string, live: p["day_pnl"]?.number != nil) {
                    StatusPill(text: "Stale · as of \(p["as_of"]?.string ?? "—")", systemImage: "clock.badge.exclamationmark", color: .tdWarn)
                }
                if let d = p["day_pnl"]?.number {
                    PnlLine(label: "PAPER today", value: d, big: true)
                } else if let d = p["last_day_pnl"]?.number {
                    PnlLine(label: "PAPER last day · \(Fmt.marketDay(p["day_date"]?.string))", value: d, big: true)
                }
                PnlLine(label: "PAPER total", value: p["cum_net"]?.number ?? 0)
                ForEach(Array((p["kill"]?.array ?? []).enumerated()), id: \.offset) { _, k in
                    Label("Kill rule paused \(k["name"]?.string ?? "a session")", systemImage: "exclamationmark.octagon.fill")
                        .font(.subheadline).foregroundStyle(Color.tdBad)
                }
            } else if !paperError && paper != nil {
                Text("No paper sessions yet. Start one from Trade once a strategy reaches the paper stage.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Button("Open Trade") { model.tab = .trade }.font(.subheadline.weight(.semibold))
            Text("Live trading is in Orders. Keep Algo Lab off live on the same Zerodha account.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}
