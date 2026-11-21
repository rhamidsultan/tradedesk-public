import SwiftUI

/// Templates: the ready-made strategies, apart from the trader's own. Grouped by kind until the trader arranges
/// them (Reorder, or swipe right › Move to top); then in their order, kept on the server for this login.
struct TemplatesView: View {
    @Environment(AppModel.self) private var model
    let templates: [StrategyTemplate]
    let gallery: [JSONValue]
    let open: (StrategyRoute) -> Void
    let customise: (String?) async -> Void
    @State private var order: [JSONValue] = []
    @State private var editMode: EditMode = .inactive

    private var arranged: Bool { !(model.lists["templates"] ?? []).isEmpty }

    var body: some View {
        List {
            if arranged || editMode.isEditing {
                Section {
                    ForEach(order, id: \.self) { g in row(g, showCategory: true) }
                        .onMove { order.move(fromOffsets: $0, toOffset: $1); save() }
                } footer: {
                    if !editMode.isEditing {
                        Button("Back to grouped by kind") { Task { await model.saveList("templates", []); reload() } }
                            .font(.footnote)
                    }
                }
            } else {
                let groups = Dictionary(grouping: order) { $0["category"]?.string ?? "Other" }
                ForEach(groups.keys.sorted(), id: \.self) { cat in
                    Section(cat) { ForEach(groups[cat] ?? [], id: \.self) { g in row(g, showCategory: false) } }
                }
            }
        }
        .environment(\.editMode, $editMode)
        .navigationTitle("Templates")
        .toolbar {
            if order.count > 1 {
                Button(editMode.isEditing ? "Done" : "Reorder") {
                    withAnimation {
                        if !editMode.isEditing && !arranged { order = grouped() }   // start from what is on screen
                        editMode = editMode.isEditing ? .inactive : .active
                    }
                }
            }
        }
        .onAppear { reload() }
    }

    private func row(_ g: JSONValue, showCategory: Bool) -> some View {
        let t = templates.first { $0.id == g["id"]?.string }
        return VStack(alignment: .leading, spacing: 4) {
            Text(g["name"]?.string ?? "").font(.headline)
            HStack(spacing: 6) {
                if showCategory, let c = g["category"]?.string {
                    Text(c).font(.caption2.weight(.semibold)).foregroundStyle(.tint)
                }
                ForEach((g["chips"]?.array ?? []).compactMap(\.string), id: \.self) { c in
                    Text(c).font(.caption2.weight(.semibold)).padding(.horizontal, 7).padding(.vertical, 3)
                        .background(.quaternary, in: .capsule)
                }
            }
            if let t { Text("\(t.underlying) · stop \(stopText(t)) · max loss a day \(Fmt.rupees(t.maxLossPerDay))").font(.caption).foregroundStyle(.secondary) }
            if !editMode.isEditing {
                HStack {
                    Button("Customise") { Task { await customise(g["id"]?.string) } }.buttonStyle(.glass)
                    if let t { Button("Backtest") { open(.template(t)) }.buttonStyle(.glass) }
                    if let t {
                        Menu {
                            Button("Find best settings", systemImage: "slider.horizontal.below.square.and.square.filled") { open(.sweep(t.id)) }
                            Button("Replay a day", systemImage: "play.circle") { open(.replay(t.id)) }
                        } label: { Image(systemName: "ellipsis.circle") }
                        .accessibilityLabel("More for \(t.name)")
                    }
                }
                .font(.subheadline)
            }
        }
        .padding(.vertical, 2)
        .swipeActions(edge: .leading) {
            if order.first != g {
                Button("Move to top", systemImage: "arrow.up.to.line") {
                    if !arranged { order = grouped() }
                    if let i = order.firstIndex(of: g) { withAnimation { order.move(fromOffsets: IndexSet(integer: i), toOffset: 0) } }
                    save()
                }.tint(.blue)
            }
        }
    }

    /// The default: grouped by kind, kinds A–Z.
    private func grouped() -> [JSONValue] {
        let groups = Dictionary(grouping: gallery) { $0["category"]?.string ?? "Other" }
        return groups.keys.sorted().flatMap { groups[$0] ?? [] }
    }

    private func reload() {
        order = arranged ? model.arranged(gallery, "templates") { $0["id"]?.string ?? "" } : gallery
    }

    private func save() {
        let ids = order.compactMap { $0["id"]?.string }
        Task { await model.saveList("templates", ids) }
    }

    private func stopText(_ t: StrategyTemplate) -> String {
        guard let s = t.stopLoss, let v = s.value else { return "set" }
        switch s.type { case "multiple": return "\(v.formatted())× premium"; case "pct": return "\(v.formatted())%"
        default: return "\(v.formatted()) \(s.type ?? "")" }
    }
}
