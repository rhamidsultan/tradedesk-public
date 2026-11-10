import CryptoKit
import XCTest
@testable import TradeDesk

final class CoreTests: XCTestCase {
    func testSignMessagesMatchServer() {
        // must match tdserver/devicekeys.py byte for byte
        XCTAssertEqual(String(decoding: SignMessage.enroll(code: "K7QM-2XHD-9RTB"), as: UTF8.self), "TD2 enroll v1|K7QM-2XHD-9RTB")
        XCTAssertEqual(String(decoding: SignMessage.login(challengeID: "abc", nonce: "Tm9uY2U="), as: UTF8.self),
                       "TD2 login v1|abc|Tm9uY2U=")
    }

    func testDeviceKeySignatureVerifies() throws {
        let priv = P256.Signing.PrivateKey()
        let key = DeviceKey(testKey: priv)
        XCTAssertEqual(key.publicKeyX963.count, 65)
        XCTAssertEqual(key.keyID.count, 64)
        let msg = SignMessage.login(challengeID: "c1", nonce: "bm9uY2U=")
        let der = try key.signature(for: msg, reason: "test")
        let sig = try P256.Signing.ECDSASignature(derRepresentation: der)
        XCTAssertTrue(priv.publicKey.isValidSignature(sig, for: msg))
    }

    func testInviteAndServerNormalising() {
        XCTAssertEqual(AppModel.normaliseInvite("k7qm 2xhd 9rtb"), "K7QM-2XHD-9RTB")
        XCTAssertEqual(AppModel.normalise("trade.example.com/")?.absoluteString, "https://trade.example.com")
        XCTAssertNil(AppModel.normalise("http://trade.example.com"))       // plain http only on the local network
        XCTAssertNotNil(AppModel.normalise("http://192.168.1.20:8000"))
        XCTAssertEqual(AppModel.normalise("192.168.1.20:8000")?.absoluteString, "http://192.168.1.20:8000")   // Mac trial
        XCTAssertEqual(AppModel.normalise("10.0.0.5:8000")?.absoluteString, "http://10.0.0.5:8000")
    }

    func testStatusDecodes() throws {
        let json = """
        {"server_time":"2026-11-03T04:30:00.5+00:00","server_time_ist":"2026-11-03T10:00:00.5+05:30","version":"0.1.0",
         "market":{"state":"open","open":"2026-11-03T09:15:00+05:30","fo_close":"2026-11-03T15:40:00+05:30",
                   "cash_close":"2026-11-03T15:35:00+05:30"},
         "kite":{"set_up":true,"client_id":"AB••34","connected_today":false,"last_login_at":null,"token_day":null},
         "alerts":{"open":2,"critical":{"id":7,"title":"Feed stale"}},
         "paper":{"available":false,"sessions":0},"live":{"available":false,"armed":false,"sessions":0}}
        """
        let s = try APIClient.decoder.decode(StatusSnapshot.self, from: Data(json.utf8))
        XCTAssertEqual(s.market.state, "open")
        XCTAssertEqual(s.alerts.critical?.title, "Feed stale")
        XCTAssertFalse(s.kite.connectedToday)
    }

    func testProvisioningExpiry() {
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict><key>ExpirationDate</key><date>2026-11-10T08:00:00Z</date></dict></plist>
        """
        var blob = Data([0x30, 0x82, 0x01, 0x00])      // CMS bytes around the plist
        blob.append(Data(plist.utf8))
        blob.append(Data([0x00, 0xA0, 0x82]))
        let exp = Provisioning.expiryDate(fromProfile: blob)
        XCTAssertEqual(exp, ServerDate.parse("2026-11-10T08:00:00+00:00"))
        XCTAssertEqual(Provisioning.daysLeft(until: exp!, now: ServerDate.parse("2026-11-03T08:00:00+00:00")!), 7)
    }

    @MainActor
    func testDeepLinks() {
        let m = AppModel()
        m.open(URL(string: "tradedesk://broker/kite")!)
        XCTAssertEqual(m.tab, .more)
        XCTAssertEqual(m.morePath, [.broker])
        m.open(URL(string: "tradedesk://alerts")!)
        XCTAssertEqual(m.tab, .home)
        XCTAssertEqual(m.homePath, [.alerts])
    }

    func testDataModelsDecode() throws {
        let month = """
        {"underlying":"NIFTY","month":"2026-10","summary":{"ok":1,"warn":1,"fail":0},
         "days":[{"date":"2026-10-15","trading_day":true,"spot_bars":375,"vix":true,"option_contracts":3,"option_bars":1125,
                  "status":"ok","issues":[]},
                 {"date":"2026-10-16","trading_day":true,"spot_bars":375,"vix":false,"option_contracts":3,"option_bars":1125,
                  "status":"warn","issues":[{"level":"warn","text":"No India VIX"}]}]}
        """
        let m = try APIClient.decoder.decode(DataMonth.self, from: Data(month.utf8))
        XCTAssertEqual(m.days[1].issues.first?.text, "No India VIX")
        let chain = """
        {"underlying":"NIFTY","expiry":"2026-10-20","date":"2026-10-15","at":"09:20","spot":25005.0,
         "strikes":[{"strike":25000.0,"CE":{"ltp":120.05,"volume":390,"oi":1005,"as_of":"2026-10-15 09:20:00"},"PE":null}]}
        """
        let c = try APIClient.decoder.decode(OptionChain.self, from: Data(chain.utf8))
        XCTAssertEqual(c.strikes[0].CE?.ltp, 120.05)
        XCTAssertNil(c.strikes[0].PE)
        let bar = Bar(ts: "2026-10-15 09:15:00", o: 1, h: 1, l: 1, c: 1, v: nil, oi: nil)
        XCTAssertEqual(bar.date, ServerDate.parse("2026-10-15T03:45:00+00:00"))
        XCTAssertEqual(Fmt.marketDay("2026-10-19"), "19 Oct 2026")
    }

    func testResearchModelsDecode() throws {
        let detail = """
        {"id":3,"name":"Strangle","template":"strangle_nifty","underlying":"NIFTY","start":"2026-10-12","end":"2026-10-13",
         "status":"done","progress":100,"note":"","starred":false,"archived":false,"fill_model":"honest",
         "created_at":"2026-11-03T12:00:00.123456+00:00","finished_at":"2026-11-03T12:01:00+00:00",
         "summary":{"net_pnl":-1250.5,"trades":1,"luck_warning":true},
         "metrics":{"net_pnl":-1250.5,"gross_pnl":-1100,"charges":150.5,"trades":1,"max_drawdown":1300,"by_year":{"2026":-1250.5},
                    "days":2,"by_exit_reason":{"stop_loss":{"trades":1,"net":-1250.5}},
                    "monte_carlo":null,"net_with_charges_x1_5":-1325.75,"fills_over_10pct_volume":0,"luck_warning":true,
                    "worst_day":{"date":"2026-10-12","pnl":-1300},"worst_week":{"week":"2026-W42","pnl":-1250}},
         "audit":{"dates":{"asked":["2026-10-12","2026-10-13"],"with_data":2,"first":"2026-10-12","last":"2026-10-13"},
                  "fills":{"total":4,"stale":0,"real_price_pct":100.0,"over_10pct_of_volume":0},
                  "fill_model":{"fill_model":"honest","slippage_pct":0.5,"min_half_spread":[[20,0.1],[100,0.2]],
                                "fill_max_age_min":3,"cost_stress":1.0,"sl_order":"assume_fill","stop_fill_frac":0.5},
                  "exits_at_old_price":0,"lot_sizes_by_expiry":{"2026-10-13":65},
                  "charges_rates":{"2026-10-12":{"stt_sell_pct":0.15}},"skipped":{},"engine_version":"dev","spec_hash":"abc"},
         "spec":{}}
        """
        let d = try APIClient.decoder.decode(BacktestDetail.self, from: Data(detail.utf8))
        XCTAssertEqual(d.run.fillModel, "honest")
        XCTAssertEqual(d.metrics?.netWithChargesX15, -1325.75)
        XCTAssertEqual(d.metrics?.byExitReason?.keys.first.map(humanize), "Stop loss")
        XCTAssertEqual(d.audit?.lotSizesByExpiry["2026-10-13"] ?? nil, 65)
        XCTAssertEqual(humanize("entry_skipped_no_price"), "Entry skipped no price")
        let strip = """
        {"indexes":[{"underlying":"NIFTY","price":25502.5,"as_of":"2026-11-03T10:00:00+05:30","live":true,
                     "prev_close":25250.0,"change_pct":1.0}],"feed":{"live":true,"last_tick":null,"stale":false}}
        """
        let s = try APIClient.decoder.decode(IndexStrip.self, from: Data(strip.utf8))
        XCTAssertEqual(s.indexes.first?.changePct, 1.0)
    }
}
