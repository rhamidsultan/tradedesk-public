import SwiftUI

/// Drag to arrange a short list (Home cards, index tiles); Done saves this login's order on the server.
struct ArrangeSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let title: String
    let list: String
    @State var items: [ArrangeItem]
    var footer: String? = nil

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(items) { it in Text(it.label) }
                        .onMove { items.move(fromOffsets: $0, toOffset: $1) }
                } footer: { Text(footer ?? "Drag ≡ to change the order.") }
            }
            .environment(\.editMode, .constant(.active))
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        let ids = items.map(\.id)
                        Task { await model.saveList(list, ids); dismiss() }
                    }
                }
                ToolbarItem(placement: .bottomBar) {
                    Button("Reset to default", role: .destructive) {
                        Task { await model.saveList(list, []); dismiss() }
                    }
                }
            }
        }
    }
}

struct ArrangeItem: Identifiable {
    let id: String
    let label: String
}
