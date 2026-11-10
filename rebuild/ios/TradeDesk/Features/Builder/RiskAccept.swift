import SwiftUI

/// A change that raises risk above the 2% / 4% marks (a preset, a sweep row saved as a preset) is refused by the
/// server with the amount to accept. This shows the amounts, always in ₹, and retries with that acceptance.
struct PendingAccept: Identifiable {
    let id = UUID()
    let amount: Double
    let items: [(what: String, amount: Double)]
    let retry: (Double) async -> Void

    /// The acceptance asked for by a 409 answer, if that is what the error is.
    static func from(_ error: Error, retry: @escaping (Double) async -> Void) -> PendingAccept? {
        guard case APIError.http(409, _, let data) = error,
              let v = try? JSONValue.rawDecoder.decode(JSONValue.self, from: data),
              let need = v["detail"]?["accept_required"]?.number else { return nil }
        let items = (v["detail"]?["items"]?.array ?? []).map { ($0["what"]?.string ?? "", $0["amount"]?.number ?? 0) }
        return PendingAccept(amount: need, items: items, retry: retry)
    }
}

struct RiskAcceptSheet: View {
    let pending: PendingAccept
    @Environment(\.dismiss) private var dismiss
    @State private var sending = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    ForEach(Array(pending.items.enumerated()), id: \.offset) { _, i in
                        LabeledContent(i.what) { Text(Fmt.rupees(i.amount)).foregroundStyle(Color.tdBad) }
                    }
                } header: { Text("These settings go above your marks") } footer: {
                    Text("Accepting is recorded with the preset. Paper and live still stop at the strategy's max loss a day.")
                }
                .monospacedDigit()
                Section {
                    Button(role: .destructive) {
                        Task { sending = true; await pending.retry(pending.amount); sending = false; dismiss() }
                    } label: {
                        HStack { Text("Accept up to \(Fmt.rupees(pending.amount))"); if sending { Spacer(); ProgressView() } }
                    }
                    .disabled(sending)
                }
            }
            .navigationTitle("Accept the risk?")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
        .presentationDetents([.medium, .large])
    }
}
