import SwiftUI

/// Liquid Glass building blocks (iOS 26). Glass is for controls and floating layers; content cards use it
/// lightly so numbers stay readable.
struct GlassCard<Content: View>: View {
    var tint: Color? = nil
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) { content }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .glassEffect(tint.map { Glass.regular.tint($0.opacity(0.18)) } ?? .regular, in: .rect(cornerRadius: 24))
    }
}

struct CardHeader: View {
    let title: String
    let systemImage: String
    var trailing: String? = nil

    var body: some View {
        HStack {
            Label(title, systemImage: systemImage).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            Spacer()
            if let trailing { Text(trailing).font(.caption).monospacedDigit().foregroundStyle(.secondary) }
        }
    }
}

/// Small capsule with an icon and a word, e.g. "● Open", "Paper", "LIVE".
struct StatusPill: View {
    let text: String
    let systemImage: String
    var color: Color = .secondary

    var body: some View {
        Label(text, systemImage: systemImage)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .glassEffect(.regular.tint(color.opacity(0.15)), in: .capsule)
            .accessibilityElement(children: .combine)
    }
}

/// Shown on tabs whose features arrive in later phases, listing exactly what will come.
struct ComingInPhase: View {
    let title: String
    let phase: String
    let systemImage: String
    let items: [String]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    GlassCard {
                        Label(phase, systemImage: "calendar.badge.clock").font(.headline)
                        Text("This tab is part of the rebuild plan. Today's Algo Lab and TradeDesk keep running until it is ready.")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    GlassCard {
                        CardHeader(title: "What arrives here", systemImage: systemImage)
                        ForEach(items, id: \.self) { item in
                            Label(item, systemImage: "circle.dashed").font(.subheadline)
                        }
                    }
                }
                .padding()
            }
            .background(AppBackground())
            .navigationTitle(title)
        }
    }
}
