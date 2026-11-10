import SwiftUI

/// The glass strip above the tab bar: market state, Kite login, alerts. Always visible, tap to act.
struct StatusStrip: View {
    @Environment(AppModel.self) private var model
    @AppStorage("showIST") private var showIST = true

    var body: some View {
        HStack(spacing: 14) {
            if model.status == nil || model.isStale {
                stale
            } else {
                market
            }
            if trades {                 // only logins that trade need their own Zerodha login each day
                Divider().frame(height: 18)
                kite
            }
            Spacer(minLength: 0)
            if let l = model.status?.live, (l.armed == true || (l.openLegs ?? 0) > 0) { live(l) }
            alerts
        }
        .font(.footnote.weight(.semibold))
        .monospacedDigit()
        .lineLimit(1)
        .padding(.horizontal, 16)
        .opacity(model.isStale && model.status != nil ? 0.75 : 1)
    }

    private var trades: Bool { model.can("paper", "use") || model.can("live", "use") }

    private var stale: some View {
        let text: String
        if model.status == nil {
            text = "No server"
        } else if let age = model.dataAge {
            text = age < 120 ? "Stale · \(Int(age))s" : "Stale · \(Int(age / 60))m"
        } else {
            text = "Stale"
        }
        return Label(text, systemImage: "wifi.slash")
            .foregroundStyle(.secondary)
            .accessibilityLabel(model.status == nil ? "Cannot reach the server" : "Data is out of date. \(text)")
    }

    private var market: some View {
        let state = model.status?.market.state ?? "unknown"
        let (text, color): (String, Color) = switch state {
        case "open": ("Market open", .tdGood)
        case "pre_open": ("Pre-open", .tdWarn)
        case "fo_only": ("F&O only", .tdWarn)
        case "closed": ("Closed", .secondary)
        default: ("Unknown", .secondary)
        }
        return ViewThatFits {
            Label(text, systemImage: "circle.fill").labelStyle(DotLabel(color: color))
            Label(text.components(separatedBy: " ").last ?? text, systemImage: "circle.fill").labelStyle(DotLabel(color: color))
        }
            .accessibilityLabel("Market \(text)")
    }

    private var kite: some View {
        let k = model.status?.kite
        let ok = k?.connectedToday == true
        return Button {
            model.tab = .more; model.morePath = [.broker]
        } label: {
            Label(ok ? "Kite" : "Kite login", systemImage: ok ? "checkmark.seal.fill" : "key.fill")
                .foregroundStyle(ok ? Color.tdGood : Color.tdWarn)
                .frame(minHeight: 44)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(ok ? "Kite logged in today" : "Kite login needed")
    }

    /// LIVE armed or open, from anywhere: one tap to the Orders tab (and Stop all).
    private func live(_ l: StatusSnapshot.Engine) -> some View {
        let n = l.openLegs ?? 0
        let down = l.runnerOk == false
        return Button {
            model.ordersMode = "live"; model.tab = .orders
        } label: {
            Label(n > 0 ? "LIVE · \(n)" : "LIVE", systemImage: down ? "exclamationmark.triangle.fill" : "bolt.fill")
                .foregroundStyle(down ? Color.tdBad : Color.tdLive)
                .frame(minHeight: 44)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Live trading \(l.armed == true ? "armed" : "not armed"), \(n) open legs\(down ? ", live runner not answering" : ""). Opens Orders.")
    }

    private var alerts: some View {
        let n = model.status?.alerts.open ?? 0
        let critical = model.status?.alerts.critical != nil
        return Button {
            model.tab = .home; model.homePath = [.alerts]
        } label: {
            Label("\(n)", systemImage: critical ? "bell.badge.fill" : "bell")
                .foregroundStyle(critical ? Color.tdBad : n > 0 ? Color.tdWarn : .secondary)
                .symbolEffect(.bounce, value: critical)
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(critical ? "\(n) open alerts, one critical" : "\(n) open alerts")
    }
}

private struct DotLabel: LabelStyle {
    let color: Color
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            configuration.title
        }
    }
}
