import SwiftUI

struct AlertsView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true
    @State private var all: [AlertItem] = []
    @State private var showOpenOnly = true
    @State private var loadError: String?
    @State private var confirmAll = false

    var body: some View {
        List {
            Picker("Show", selection: $showOpenOnly) {
                Text("Open").tag(true)
                Text("All").tag(false)
            }
            .pickerStyle(.segmented)
            .listRowBackground(Color.clear)

            let items = showOpenOnly ? model.openAlerts : all
            if let loadError {
                Label {
                    VStack(alignment: .leading) {
                        Text("Couldn't reach the server").font(.headline)
                        Text(loadError).font(.footnote).foregroundStyle(.secondary)
                        Button("Retry") { Task { await load() } }.buttonStyle(.glass)
                    }
                } icon: { Image(systemName: "wifi.slash") }
            } else if items.isEmpty {
                ContentUnavailableView("No alerts", systemImage: "bell.slash", description: Text("You're all caught up."))
            }
            ForEach(items) { a in
                VStack(alignment: .leading, spacing: 6) {
                    Label(a.title, systemImage: Level.icon(a.level))
                        .font(.headline).foregroundStyle(Level.color(a.level))
                    if !a.body.isEmpty { Text(a.body).font(.subheadline) }
                    HStack {
                        Text(Fmt.dayTime(a.createdAt, inIST: showIST))
                        Text("·")
                        Text(a.category.replacingOccurrences(of: "_", with: " "))
                        if a.repeating { Label("repeating", systemImage: "repeat").labelStyle(.titleAndIcon) }
                        if let via = a.ackedVia { Text("· done (\(via))") }
                    }
                    .font(.caption).foregroundStyle(.secondary)
                    if a.ackedAt == nil {
                        Button("Acknowledge", systemImage: "checkmark") { Task { await ack(a) } }
                            .buttonStyle(.glass).controlSize(.regular)
                    }
                }
                .padding(.vertical, 4)
                .swipeActions {
                    if a.ackedAt == nil {
                        Button("Acknowledge", systemImage: "checkmark") { Task { await ack(a) } }.tint(.tdGood)
                    }
                }
                .contextMenu {
                    if a.ackedAt == nil { Button("Acknowledge", systemImage: "checkmark") { Task { await ack(a) } } }
                }
            }
        }
        .scrollContentBackground(.hidden)
        .background(AppBackground())
        .navigationTitle("Alerts")
        .refreshable { await load() }
        .task(id: showOpenOnly) { await load() }
        .toolbar {
            if showOpenOnly && !model.openAlerts.isEmpty {
                Button("Acknowledge all") { confirmAll = true }
            }
        }
        .confirmationDialog(allTitle, isPresented: $confirmAll, titleVisibility: .visible) {
            Button("Acknowledge all") { Task { await model.ack(model.openAlerts); await load() } }
        } message: {
            Text("Repeating messages stop for all of them.")
        }
    }

    private func ack(_ a: AlertItem) async {
        await model.ack(a)
        await load()
    }

    private var allTitle: String {
        let n = model.openAlerts.count, c = model.openAlerts.filter { $0.level == "critical" }.count
        return c > 0 ? "Acknowledge \(n) alerts (\(c) critical)?" : "Acknowledge \(n) alerts?"
    }

    private func load() async {
        do {
            try await model.refresh()
            if !showOpenOnly { all = try await model.api.get("/v1/alerts?limit=100") }
            loadError = nil
        } catch {
            loadError = error.localizedDescription
        }
    }
}
