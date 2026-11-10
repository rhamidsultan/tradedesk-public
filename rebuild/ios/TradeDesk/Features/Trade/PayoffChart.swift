import Charts
import SwiftUI

/// One open option leg for the payoff chart.
struct PayoffLeg {
    let strike: Double
    let isCall: Bool
    let short: Bool
    let qty: Double
    let entry: Double

    init?(json p: JSONValue) {
        guard let k = p["strike"]?.number, let q = p["qty"]?.number, let e = p["entry"]?.number,
              let t = p["type"]?.string else { return nil }
        strike = k; isCall = t == "CE"; short = p["side"]?.string == "SELL"; qty = q; entry = e
    }

    init(strike: Double, isCall: Bool, short: Bool, qty: Double, entry: Double) {
        self.strike = strike; self.isCall = isCall; self.short = short; self.qty = qty; self.entry = entry
    }

    /// P&L at expiry if the index ends at `s` (before charges).
    func pnl(at s: Double) -> Double {
        let intrinsic = isCall ? max(0, s - strike) : max(0, strike - s)
        return (short ? entry - intrinsic : intrinsic - entry) * qty
    }
}

enum Payoff {
    /// Points across a range that covers every strike with room either side.
    static func curve(_ legs: [PayoffLeg], steps: Int = 120) -> [(x: Double, y: Double)] {
        guard let lo = legs.map(\.strike).min(), let hi = legs.map(\.strike).max() else { return [] }
        let pad = max((hi - lo) * 0.6, lo * 0.03)
        let a = lo - pad, b = hi + pad
        return (0...steps).map { i in
            let x = a + (b - a) * Double(i) / Double(steps)
            return (x, legs.reduce(0) { $0 + $1.pnl(at: x) })
        }
    }

    /// Index levels where the payoff crosses zero.
    static func breakevens(_ pts: [(x: Double, y: Double)]) -> [Double] {
        zip(pts, pts.dropFirst()).compactMap { p, q in
            guard (p.y < 0) != (q.y < 0), q.y != p.y else { return nil }
            return p.x + (q.x - p.x) * (0 - p.y) / (q.y - p.y)
        }
    }
}

struct PayoffChart: View {
    let legs: [PayoffLeg]

    var body: some View {
        let pts = Payoff.curve(legs)
        let bes = Payoff.breakevens(pts)
        VStack(alignment: .leading, spacing: 6) {
            Chart {
                ForEach(Array(pts.enumerated()), id: \.offset) { _, p in
                    AreaMark(x: .value("Index", p.x), y: .value("P&L", p.y))
                        .foregroundStyle(p.y >= 0 ? Color.tdGood.opacity(0.25) : Color.tdBad.opacity(0.25))
                    LineMark(x: .value("Index", p.x), y: .value("P&L", p.y)).foregroundStyle(Color.primary)
                }
                RuleMark(y: .value("Zero", 0)).foregroundStyle(.secondary)
                ForEach(bes, id: \.self) { b in
                    RuleMark(x: .value("Breakeven", b)).foregroundStyle(Color.tdWarn).lineStyle(StrokeStyle(dash: [4, 3]))
                }
            }
            .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if let d = v.as(Double.self) { Text(Fmt.rupeesShort(d)) } } } }
            .accessibilityLabel("Payoff at expiry")
            if !bes.isEmpty {
                Text("Breakeven at expiry: " + bes.map { $0.formatted(.number.precision(.fractionLength(0))) }.joined(separator: " and "))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
