import ActivityKit
import Foundation

/// The Live Activity / Dynamic Island for paper sessions, shared by the app and the widget extension.
/// The app starts and updates it while it runs (no push updates yet); it shows PAPER so it is never read as
/// real money.
struct PaperActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var dayPnl: Double
        var sessions: Int
        var openLegs: Int
        var nearestStopPct: Double?
        var asOf: String
        var kill: Bool
    }

    var title: String
}

enum ActivityText {
    /// ₹ in the Indian format with a sign, no decimals (widget-side copy of Fmt.rupees).
    static func rupees(_ v: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.locale = Locale(identifier: "en_IN")
        f.maximumFractionDigits = 0
        let s = f.string(from: NSNumber(value: abs(v).rounded())) ?? "\(Int(abs(v)))"
        return (v > 0 ? "+" : v < 0 ? "−" : "") + "₹" + s
    }

    static func stop(_ pct: Double?) -> String {
        guard let pct else { return "No open stops" }
        return "Stop \(Int(pct.rounded()))% away"
    }
}
