import ActivityKit
import SwiftUI
import WidgetKit

@main
struct TradeDeskWidgets: WidgetBundle {
    var body: some Widget { PaperLiveActivity() }
}

private let paperBlue = Color(red: 0.56, green: 0.64, blue: 1.0)

struct PaperLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: PaperActivityAttributes.self) { ctx in
            LockScreenView(state: ctx.state, title: ctx.attributes.title, stale: ctx.isStale)
                .padding()
                .activityBackgroundTint(Color.black.opacity(0.6))
        } dynamicIsland: { ctx in
            let s = ctx.state
            let stale = ctx.isStale
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label("PAPER", systemImage: "doc.text").font(.caption.weight(.bold)).foregroundStyle(paperBlue)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    VStack(alignment: .trailing, spacing: 0) {
                        Text(ActivityText.rupees(s.dayPnl)).font(.headline).monospacedDigit()
                            .foregroundStyle(stale ? .secondary : pnlColor(s.dayPnl))
                        Text("today").font(.caption2).foregroundStyle(.secondary)
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Text("\(s.sessions) session\(s.sessions == 1 ? "" : "s") · \(s.openLegs) open legs")
                        Spacer()
                        Text(s.kill ? "Kill rule" : ActivityText.stop(s.nearestStopPct))
                            .foregroundStyle(s.kill || (s.nearestStopPct ?? 100) < 20 ? .orange : .secondary)
                    }
                    .font(.caption)
                    Label(stale ? "Not updating · open TradeDesk · last \(s.asOf)" : "As of \(s.asOf) IST",
                          systemImage: stale ? "clock.badge.exclamationmark" : "clock")
                        .font(.caption2).foregroundStyle(stale ? .orange : .secondary)
                }
            } compactLeading: {
                Text("PAPER").font(.caption2.bold()).foregroundStyle(paperBlue)
            } compactTrailing: {
                Text(stale ? "—" : ActivityText.rupees(s.dayPnl)).monospacedDigit().font(.caption)
                    .foregroundStyle(stale ? .secondary : pnlColor(s.dayPnl))
            } minimal: {
                Image(systemName: s.kill ? "exclamationmark.octagon.fill" : "doc.text")
                    .foregroundStyle(s.kill ? .orange : paperBlue)
            }
        }
    }
}

private func pnlColor(_ v: Double) -> Color { v > 0 ? .green : v < 0 ? .red : .secondary }

private struct LockScreenView: View {
    let state: PaperActivityAttributes.ContentState
    let title: String
    let stale: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label("PAPER · \(title)", systemImage: "doc.text").font(.caption.weight(.bold)).foregroundStyle(paperBlue)
                Spacer()
                Label(stale ? "Not updating · last \(state.asOf)" : "as of \(state.asOf) IST",
                      systemImage: stale ? "clock.badge.exclamationmark" : "clock")
                    .font(.caption2).foregroundStyle(stale ? .orange : .secondary)
            }
            HStack(alignment: .firstTextBaseline) {
                Text("Today").font(.subheadline).foregroundStyle(.secondary)
                Spacer()
                Text(ActivityText.rupees(state.dayPnl)).font(.title2.weight(.semibold)).monospacedDigit()
                    .foregroundStyle(stale ? .secondary : pnlColor(state.dayPnl))
            }
            if stale { Text("Open TradeDesk to update.").font(.caption2).foregroundStyle(.orange) }
            HStack {
                Text("\(state.sessions) session\(state.sessions == 1 ? "" : "s") · \(state.openLegs) open legs")
                Spacer()
                Text(state.kill ? "Kill rule fired" : ActivityText.stop(state.nearestStopPct))
                    .foregroundStyle(state.kill || (state.nearestStopPct ?? 100) < 20 ? .orange : .secondary)
            }
            .font(.caption)
        }
    }
}
