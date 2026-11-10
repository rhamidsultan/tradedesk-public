import XCTest
@testable import TradeDesk

final class FormattersTests: XCTestCase {
    func testIndianGrouping() {
        XCTAssertEqual(Fmt.rupees(0), "₹0")
        XCTAssertEqual(Fmt.rupees(999), "₹999")
        XCTAssertEqual(Fmt.rupees(1000), "₹1,000")
        XCTAssertEqual(Fmt.rupees(125000), "₹1,25,000")
        XCTAssertEqual(Fmt.rupees(12345678), "₹1,23,45,678")
        XCTAssertEqual(Fmt.rupees(-4250.5, decimals: 2), "−₹4,250.50")
        XCTAssertEqual(Fmt.rupees(1500, signed: true), "+₹1,500")
        XCTAssertEqual(Fmt.rupees(125000, hidden: true), "₹ ••••")
    }

    func testShortForm() {
        XCTAssertEqual(Fmt.rupeesShort(125000), "₹1.25 L")
        XCTAssertEqual(Fmt.rupeesShort(34000000), "₹3.4 Cr")
        XCTAssertEqual(Fmt.rupeesShort(9500), "₹9,500")
    }

    func testISTTimes() {
        let d = ServerDate.parse("2026-11-03T04:30:00+00:00")!
        XCTAssertEqual(Fmt.time(d), "10:00 IST")
        XCTAssertEqual(Fmt.day(d), "3 Nov 2026")
    }

    func testServerDates() {
        XCTAssertNotNil(ServerDate.parse("2026-11-03T04:30:00.123456+00:00"))
        XCTAssertNotNil(ServerDate.parse("2026-11-03T10:00:00+05:30"))
        XCTAssertEqual(ServerDate.parse("2026-11-03T10:00:00+05:30"), ServerDate.parse("2026-11-03T04:30:00+00:00"))
        XCTAssertNil(ServerDate.parse("not a date"))
    }

    func testContractAndMonthLabels() {
        XCTAssertEqual(Fmt.contract("NIFTY26102025000CE"), "NIFTY 25,000 CE · 20 Oct")
        XCTAssertEqual(Fmt.contract("BANKNIFTY261124" + "55500PE"), "BANKNIFTY 55,500 PE · 24 Nov")
        XCTAssertEqual(Fmt.contract("NIFTY"), "NIFTY")
        XCTAssertEqual(Fmt.month("2026-10"), "Oct 2026")
        XCTAssertEqual(Fmt.storeTime("2026-10-23 09:20:05", seconds: true), "23 Oct, 09:20:05 IST")
        XCTAssertEqual(Fmt.rupees(-0.4), "₹0")
    }
}
