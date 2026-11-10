import Foundation

/// Is a paper figure behind? During market hours (Mon–Fri 09:15–15:30 IST) an "as of" more than 2 minutes behind
/// IST now means the server's replay has stalled, so the number must not look current.
enum PaperClock {
    static func minutesIST(_ date: Date = .now) -> (weekday: Int, minute: Int) {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = Fmt.ist
        let c = cal.dateComponents([.weekday, .hour, .minute], from: date)
        return (c.weekday ?? 1, (c.hour ?? 0) * 60 + (c.minute ?? 0))
    }

    static func marketOpen(_ now: Date = .now) -> Bool {
        let t = minutesIST(now)
        return (2...6).contains(t.weekday) && t.minute >= 9 * 60 + 15 && t.minute < 15 * 60 + 30
    }

    /// asOf "HH:MM" (the last closed bar); live tells whether the figure is today's intraday replay.
    static func isStale(asOf: String?, live: Bool, now: Date = .now) -> Bool {
        guard marketOpen(now), live else { return false }
        guard let asOf, asOf.count == 5, let h = Int(asOf.prefix(2)), let m = Int(asOf.suffix(2)) else { return true }
        return minutesIST(now).minute - (h * 60 + m) > 2
    }
}
