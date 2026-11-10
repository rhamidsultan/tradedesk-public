import XCTest
@testable import TradeDesk

final class PayoffTests: XCTestCase {
    func testShortStrangleKeepsBothPremiumsBetweenTheStrikes() {
        let legs = [PayoffLeg(strike: 26000, isCall: true, short: true, qty: 65, entry: 40),
                    PayoffLeg(strike: 24000, isCall: false, short: true, qty: 65, entry: 40)]
        XCTAssertEqual(legs.reduce(0) { $0 + $1.pnl(at: 25000) }, 80 * 65, accuracy: 0.001)
        // above the call strike the call loses one rupee a point a unit
        XCTAssertEqual(legs.reduce(0) { $0 + $1.pnl(at: 26100) }, (80 - 100) * 65, accuracy: 0.001)
    }

    func testBreakevensSitPremiumAwayFromTheStrikes() {
        let legs = [PayoffLeg(strike: 26000, isCall: true, short: true, qty: 65, entry: 40),
                    PayoffLeg(strike: 24000, isCall: false, short: true, qty: 65, entry: 40)]
        let be = Payoff.breakevens(Payoff.curve(legs, steps: 2400))
        XCTAssertEqual(be.count, 2)
        XCTAssertEqual(be[0], 23920, accuracy: 3)
        XCTAssertEqual(be[1], 26080, accuracy: 3)
    }

    func testLongCallLosesOnlyThePremium() {
        let leg = PayoffLeg(strike: 25000, isCall: true, short: false, qty: 65, entry: 100)
        XCTAssertEqual(leg.pnl(at: 24000), -100 * 65, accuracy: 0.001)
        XCTAssertEqual(leg.pnl(at: 25200), 100 * 65, accuracy: 0.001)
    }
}

final class PaperClockTests: XCTestCase {
    private func ist(_ s: String) -> Date {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm"; f.timeZone = Fmt.ist
        return f.date(from: s)!
    }

    func testStaleOnlyInMarketHours() {
        XCTAssertFalse(PaperClock.isStale(asOf: "10:00", live: true, now: ist("2026-11-03 10:02")))
        XCTAssertTrue(PaperClock.isStale(asOf: "10:00", live: true, now: ist("2026-11-03 10:05")))
        XCTAssertFalse(PaperClock.isStale(asOf: "15:29", live: true, now: ist("2026-11-03 18:00")))   // closed
        XCTAssertFalse(PaperClock.isStale(asOf: "10:00", live: true, now: ist("2026-11-07 10:30")))   // Saturday
    }
}
