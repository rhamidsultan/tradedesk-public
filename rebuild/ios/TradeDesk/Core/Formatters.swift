import Foundation

/// Money in Indian format (₹1,25,000), times in IST by default, dates as 3 Nov 2026 (TradeDesk conventions).
enum Fmt {
    static let ist = TimeZone(identifier: "Asia/Kolkata")!

    static func rupees(_ value: Double, decimals: Int = 0, hidden: Bool = false, signed: Bool = false) -> String {
        if hidden { return "₹ ••••" }
        let negative = value < 0
        let rounded = (abs(value) * pow(10, Double(decimals))).rounded() / pow(10, Double(decimals))
        let whole = Int(rounded)
        var digits = String(whole)
        var groups: [String] = []
        if digits.count > 3 {
            groups.append(String(digits.suffix(3)))
            digits.removeLast(3)
            while digits.count > 2 {
                groups.insert(String(digits.suffix(2)), at: 0)
                digits.removeLast(2)
            }
            if !digits.isEmpty { groups.insert(digits, at: 0) }
        } else {
            groups = [digits]
        }
        var out = "₹" + groups.joined(separator: ",")
        if decimals > 0 {
            let frac = Int(((rounded - Double(whole)) * pow(10, Double(decimals))).rounded())
            out += "." + String(format: "%0\(decimals)d", frac)
        }
        if negative && rounded > 0 { return "−" + out }
        return signed && value > 0 ? "+" + out : out
    }

    /// US dollars: "$12,345" (grouped in thousands, as the US writes it).
    static func dollars(_ value: Double, hidden: Bool = false, signed: Bool = false) -> String {
        if hidden { return "$ ••••" }
        let s = abs(value).formatted(.number.precision(.fractionLength(0)).locale(Locale(identifier: "en_US")))
        return (value < 0 ? "−" : (signed && value > 0 ? "+" : "")) + "$" + s
    }

    /// "1.25 L", "3.4 Cr" for tight spaces.
    static func rupeesShort(_ value: Double) -> String {
        let a = abs(value), sign = value < 0 ? "−" : ""
        if a >= 1_00_00_000 { return "\(sign)₹\(trim(a / 1_00_00_000)) Cr" }
        if a >= 1_00_000 { return "\(sign)₹\(trim(a / 1_00_000)) L" }
        return rupees(value)
    }

    private static func trim(_ v: Double) -> String {
        String(format: "%.2f", v).replacingOccurrences(of: #"\.?0+$"#, with: "", options: .regularExpression)
    }

    static func time(_ date: Date, inIST: Bool = true) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_IN")
        f.timeZone = inIST ? ist : .current
        f.dateFormat = "HH:mm"
        let zone = inIST ? ist : TimeZone.current
        return f.string(from: date) + " " + (zone.abbreviation(for: date) == "GMT+5:30" ? "IST" : (zone.abbreviation(for: date) ?? ""))
    }

    static func day(_ date: Date, inIST: Bool = true) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_GB")
        f.timeZone = inIST ? ist : .current
        f.dateFormat = "d MMM yyyy"
        return f.string(from: date)
    }

    /// Store timestamps and dates are naive IST: "2026-10-15 09:15:00" or "2026-10-15".
    static func parseIST(_ s: String) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = ist
        f.dateFormat = s.count > 10 ? "yyyy-MM-dd HH:mm:ss" : "yyyy-MM-dd"
        return f.date(from: String(s.prefix(19)))
    }

    /// "19 Oct 2026" from "2026-10-19" (a market date, shown as is, not converted between zones).
    static func marketDay(_ s: String?) -> String {
        guard let s, let d = parseIST(s) else { return "—" }
        return day(d, inIST: true)
    }

    /// "2026-10" -> "Oct 2026"
    static func month(_ ym: String) -> String {
        guard let d = parseIST(ym + "-01") else { return ym }
        let f = DateFormatter(); f.locale = Locale(identifier: "en_GB"); f.timeZone = ist; f.dateFormat = "MMM yyyy"
        return f.string(from: d)
    }

    /// Store/engine time text (naive IST "2026-10-23 09:20:05") -> "23 Oct, 09:20 IST"
    static func storeTime(_ s: String, seconds: Bool = false) -> String {
        guard let d = parseIST(s) else { return s }
        let f = DateFormatter(); f.locale = Locale(identifier: "en_GB"); f.timeZone = ist
        f.dateFormat = seconds ? "d MMM, HH:mm:ss" : "d MMM, HH:mm"
        return f.string(from: d) + " IST"
    }

    /// "NIFTY26102025000CE" -> "NIFTY 25,000 CE · 20 Oct"
    static func contract(_ symbol: String) -> String {
        let pattern = #"^([A-Z&-]+?)(\d{6})(\d+(?:\.\d+)?)(CE|PE)$"#
        guard let re = try? NSRegularExpression(pattern: pattern),
              let m = re.firstMatch(in: symbol, range: NSRange(symbol.startIndex..., in: symbol)), m.numberOfRanges == 5,
              let u = Range(m.range(at: 1), in: symbol), let e = Range(m.range(at: 2), in: symbol),
              let k = Range(m.range(at: 3), in: symbol), let t = Range(m.range(at: 4), in: symbol) else { return symbol }
        let ymd = String(symbol[e])
        let exp = "20\(ymd.prefix(2))-\(ymd.dropFirst(2).prefix(2))-\(ymd.suffix(2))"
        let strike = Double(symbol[k]).map { rupees($0).replacingOccurrences(of: "₹", with: "") } ?? String(symbol[k])
        let day = parseIST(exp).map { d -> String in
            let f = DateFormatter(); f.locale = Locale(identifier: "en_GB"); f.timeZone = ist; f.dateFormat = "d MMM"
            return f.string(from: d)
        } ?? exp
        return "\(symbol[u]) \(strike) \(symbol[t]) · \(day)"
    }

    static func bytes(_ n: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: n, countStyle: .file)
    }

    static func dayTime(_ date: Date, inIST: Bool = true) -> String { "\(day(date, inIST: inIST)), \(time(date, inIST: inIST))" }
}
