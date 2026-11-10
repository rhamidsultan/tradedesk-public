import SwiftUI

struct MoreView: View {
    @Environment(AppModel.self) private var model
    @State private var consoleURL: URL?
    @State private var consoleError: String?

    var body: some View {
        @Bindable var model = model
        NavigationStack(path: $model.morePath) {
            List {
                Section {
                    HStack(spacing: 14) {
                        Image(systemName: "person.crop.circle.fill").font(.largeTitle).foregroundStyle(.tint)
                        VStack(alignment: .leading) {
                            Text(model.user?.name ?? "—").font(.headline)
                            Text((model.user?.role ?? "").capitalized).font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                }
                Section("Connections") {
                    if model.can("paper", "use") || model.can("live", "use") {
                        NavigationLink(value: Route.broker) {
                            Label("Zerodha (Kite)", systemImage: "key")
                                .badge(model.status?.kite.connectedToday == true ? "Today ✓" : "Login")
                        }
                    }
                    NavigationLink(value: Route.alertSettings) { Label("Alerts: Telegram and ntfy", systemImage: "bell.and.waves.left.and.right") }
                    NavigationLink(value: Route.devices) { Label("Devices", systemImage: "iphone.and.arrow.forward") }
                }
                if model.can("portfolio") || model.can("us") {
                    Section("Portfolio") {
                        if model.can("portfolio") {
                            NavigationLink(value: Route.portfolio) { Label("India (Zerodha)", systemImage: "indianrupeesign.circle") }
                            NavigationLink(value: Route.portfolioGold) { Label("Gold", systemImage: "seal") }
                        }
                        if model.can("us") {
                            NavigationLink(value: Route.us) { Label("US stocks (IBKR)", systemImage: "dollarsign.circle") }
                        }
                    }
                }
                if model.can("paper") || model.can("live") {
                    Section("Records") {
                        NavigationLink(value: Route.journal) { Label("Trade journal", systemImage: "book.closed") }
                    }
                }
                if model.user?.role == "owner" {
                    Section("Family") {
                        NavigationLink(value: Route.users) { Label("Users and access", systemImage: "person.2") }
                        NavigationLink(value: Route.share) { Label("Share a clean copy of the code", systemImage: "shippingbox") }
                    }
                }
                if model.can("market_data") || model.can("server") {
                    Section("Server") {
                        if model.can("market_data") {
                            NavigationLink(value: Route.data) { Label("Market data", systemImage: "cylinder.split.1x2") }
                        }
                        if model.can("server") {
                            NavigationLink(value: Route.health) { Label("Server health", systemImage: "heart.text.square") }
                        }
                    }
                }
                Section {
                    Button {
                        Task { await openConsole() }
                    } label: {
                        Label("Open the web safety console", systemImage: "globe")
                    }
                    if let consoleURL {
                        ShareLink(item: consoleURL) { Label("Send link to the Mac (valid 15 min)", systemImage: "square.and.arrow.up") }
                    }
                    if let consoleError { Text(consoleError).font(.footnote).foregroundStyle(Color.tdBad) }
                } footer: {
                    Text("Use the console when this app is not installed or has expired. Stop all will work there too from phase 5.")
                }
                Section("App") {
                    NavigationLink(value: Route.settings) { Label("Settings", systemImage: "gearshape") }
                    NavigationLink(value: Route.about) {
                        Label("About this install", systemImage: "info.circle")
                            .badge(installBadge)
                    }
                }
                Section {
                    Button("Lock now", systemImage: "lock") { Task { await model.signOut() } }
                }
            }
            .scrollContentBackground(.hidden)
            .background(AppBackground())
            .navigationTitle("More")
            .navigationDestination(for: Route.self) { route in
                switch route {
                case .broker: BrokerView()
                case .alertSettings: AlertSettingsView()
                case .devices: DevicesView()
                case .about: AboutView()
                case .settings: SettingsView()
                case .alerts: AlertsView()
                case .data: DataView()
                case .recorder: RecorderView()
                case .health: HealthView()
                case .journal: JournalView()
                case .portfolio: PortfolioView()
                case .portfolioStock(let s): StockHistoryView(symbol: s, gold: false)
                case .portfolioGold: StockHistoryView(symbol: "", gold: true)
                case .us: USStocksView()
                case .users: UsersView()
                case .userDetail(let id): UserDetailView(userID: id)
                case .share: ShareCodeView()
                case .dataIndex(let u): DataIndexView(underlying: u)
                case .dataDay(let u, let d): DataDayView(underlying: u, day: d)
                case .chain(let u, let e, let d): ChainView(underlying: u, expiry: e, day: d)
                case .contract(let u, let s, let d): ContractView(underlying: u, symbol: s, day: d)
                }
            }
        }
    }

    private var installBadge: Text? {
        guard let exp = model.installExpiry else { return nil }
        return Text(Provisioning.timeLeft(until: exp))
    }

    private func openConsole() async {
        do {
            let r: LinkResponse = try await model.api.post("/v1/console/link")
            consoleURL = r.url
            consoleError = nil
            _ = await UIApplication.shared.open(r.url)
        } catch { consoleError = error.localizedDescription }
    }
}

struct SettingsView: View {
    @AppStorage("showIST") private var showIST = true
    @AppStorage("hideAmounts") private var hideAmounts = false
    @AppStorage("theme") private var theme = "system"
    @Environment(AppModel.self) private var model

    var body: some View {
        Form {
            Section {
                Toggle("Show times in IST", isOn: $showIST)
            } footer: {
                Text(showIST ? "All times are Indian Standard Time, as the exchange uses."
                             : "Times follow this iPhone (\(TimeZone.current.identifier)).")
            }
            Section {
                Toggle("Hide amounts", isOn: model.forcedHideAmounts ? .constant(true) : $hideAmounts)
                    .disabled(model.forcedHideAmounts)
            } footer: {
                Text(model.forcedHideAmounts ? "The owner set amounts to stay hidden for your login."
                     : "Shows ₹ •••• instead of amounts, for using the app in public. Example: \(Fmt.rupees(125000, hidden: hideAmounts))")
            }
            Picker("Appearance", selection: $theme) {
                Text("System").tag("system"); Text("Light").tag("light"); Text("Dark").tag("dark")
            }
        }
        .navigationTitle("Settings")
    }
}

struct AboutView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true
    @State private var confirmForget = false

    var body: some View {
        List {
            Section("This install") {
                if let exp = model.installExpiry {
                    let days = Provisioning.daysLeft(until: exp)
                    LabeledContent("Stops working", value: Fmt.dayTime(exp, inIST: false))
                    LabeledContent("Time left") {
                        Text(Provisioning.timeLeft(until: exp))
                            .foregroundStyle(days <= 2 ? Color.tdBad : days <= 3 ? Color.tdWarn : .primary)
                    }
                    Text("A free Apple account signs apps for 7 days. To renew: connect the iPhone to the same Wi-Fi as the Mac, open TradeDesk in Xcode and press Run. Your sign-in and settings stay.")
                        .font(.footnote).foregroundStyle(.secondary)
                } else {
                    Text("No expiry (simulator or a paid-account build).").foregroundStyle(.secondary)
                }
            }
            ServerVersionSection()
            Section("Versions") {
                LabeledContent("App", value: AppModel.appVersion ?? "—")
                LabeledContent("Server", value: model.status?.version ?? "—")
                LabeledContent("Server address", value: model.serverURL?.host ?? "—")
                if let t = model.status?.serverTime { LabeledContent("Server time", value: Fmt.dayTime(t, inIST: showIST)) }
            }
            Section {
                Button("Forget this device and enrol again", role: .destructive) { confirmForget = true }
                    .confirmationDialog("Forget this device?", isPresented: $confirmForget, titleVisibility: .visible) {
                        Button("Forget device", role: .destructive) { model.resetDevice() }
                    } message: {
                        Text("You will need a new invite code to sign in again on this iPhone.")
                    }
            } footer: {
                Text("Deletes the sign-in key from this iPhone. You will need a new invite code.")
            }
        }
        .navigationTitle("About")
    }
}
