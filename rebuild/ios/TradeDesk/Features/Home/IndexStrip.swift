import SwiftUI

/// NIFTY, BANKNIFTY, SENSEX, India VIX across the top of Home: live in market hours, else the last close.
struct IndexStripView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true

    var body: some View {
        if let s = model.indexes, !s.indexes.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                GlassEffectContainer(spacing: 10) {
                    HStack(spacing: 10) {
                        ForEach(s.indexes) { q in tile(q) }
                    }
                    .padding(.horizontal, 2)
                }
            }
            .scrollClipDisabled()
        }
    }

    private func tile(_ q: IndexQuote) -> some View {
        let up = (q.changePct ?? 0) > 0
        let flat = (q.changePct ?? 0) == 0
        let age = Date.now.timeIntervalSince(q.asOf)
        let vix = q.underlying == "INDIAVIX"
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
                Text(vix ? "India VIX" : q.underlying).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                if q.live { Circle().fill(Color.tdGood).frame(width: 6, height: 6).accessibilityLabel("live") }
            }
            Text(q.price.formatted(.number.precision(.fractionLength(2))))
                .font(.headline.monospacedDigit())
            if let c = q.changePct {
                Label(String(format: "%+.2f%%", c), systemImage: flat ? "minus" : up ? "arrow.up.right" : "arrow.down.right")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(vix || flat ? Color.secondary : (up ? Color.tdGood : Color.tdBad))
            }
            if q.live {
                Text(age > 30 ? "Stale \(Int(age)) s" : Fmt.time(q.asOf))
                    .font(.caption2.monospacedDigit()).foregroundStyle(age > 30 ? Color.tdWarn : .secondary)
            } else {
                Text("Close \(Fmt.day(q.asOf))").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(minWidth: 118, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 18))
        .accessibilityElement(children: .combine)
    }
}
