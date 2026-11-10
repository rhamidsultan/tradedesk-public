import Foundation

// Shapes returned by the TradeDesk server (snake_case on the wire, decoded with convertFromSnakeCase).

struct UserInfo: Codable, Equatable {
    let id: Int
    let name: String
    let role: String
}

struct EnrollResponse: Codable { let user: UserInfo; let deviceId: Int; let keyId: String }
struct ChallengeResponse: Codable { let challengeId: String; let nonce: String; let expiresAt: Date }
struct LoginResponse: Codable { let token: String; let expiresAt: Date; let user: UserInfo; let deviceId: Int }

struct StatusSnapshot: Codable, Equatable {
    struct Market: Codable, Equatable {
        let state: String          // closed | pre_open | open | fo_only
        let reason: String?
        let nextOpen: Date?
        let open: Date?
        let foClose: Date?
    }
    struct Kite: Codable, Equatable {
        let setUp: Bool
        let connectedToday: Bool
        let clientId: String?
        let lastLoginAt: Date?
    }
    struct Alerts: Codable, Equatable {
        struct Critical: Codable, Equatable { let id: Int; let title: String }
        let open: Int
        let critical: Critical?
    }
    struct Engine: Codable, Equatable {
        let available: Bool; let sessions: Int; let armed: Bool?
        var openLegs: Int? = nil; var mtm: Double? = nil; var runnerOk: Bool? = nil
    }

    let serverTime: Date
    let version: String
    let market: Market
    let kite: Kite
    let alerts: Alerts
    let paper: Engine
    let live: Engine
}

struct AlertItem: Codable, Identifiable, Equatable {
    let id: Int
    let level: String   // info | important | critical
    let category: String
    let title: String
    let body: String
    let path: String
    let createdAt: Date
    let sends: Int
    let ackedAt: Date?
    let ackedVia: String?
    let repeating: Bool
}

struct DeviceItem: Codable, Identifiable, Equatable {
    let id: Int
    let name: String
    let createdAt: Date
    let lastUsedAt: Date?
    let revoked: Bool
    let thisDevice: Bool
    let installExpiresAt: Date?
    let installDaysLeft: Int?
    let appVersion: String?
}

struct ChannelsInfo: Codable, Equatable {
    struct Channel: Codable, Equatable { let enabled: Bool; let target: String }
    let telegram: Channel?
    let ntfy: Channel?
    let telegramBotReady: Bool
    let ntfyServer: String
}

struct AlertPref: Codable, Identifiable, Equatable {
    var id: String { category }
    let category: String
    var muted: Bool
}

struct KiteStatus: Codable, Equatable {
    let setUp: Bool
    let connectedToday: Bool
    let clientId: String?
    let lastLoginAt: Date?
}

struct LoginURL: Codable { let url: URL; let callbackScheme: String }
struct LinkResponse: Codable { let url: URL }
struct CodeResponse: Codable { let code: String; let expiresInDays: Int }
struct TopicResponse: Codable { let topic: String }
struct OK: Codable { let ok: Bool }

// MARK: market data (phase 2)

struct DataSummary: Codable, Equatable {
    struct Index: Codable, Equatable, Identifiable {
        var id: String { underlying }
        let underlying: String
        let spotFrom: String?
        let spotTo: String?
        let spotDays: Int
        let optionsFrom: String?
        let optionsTo: String?
        let optionsDays: Int
        let behindDays: Int?
    }
    struct Disk: Codable, Equatable, Identifiable {
        var id: String { folder }
        let folder: String
        let bytes: Int64
    }
    let available: Bool
    let indexes: [Index]
    let vixTo: String?
    let disk: [Disk]
}

struct DataMonth: Codable, Equatable {
    struct Issue: Codable, Equatable, Hashable { let level: String; let text: String }
    struct Day: Codable, Equatable, Identifiable {
        var id: String { date }
        let date: String
        let tradingDay: Bool
        let spotBars: Int
        let vix: Bool
        let optionContracts: Int
        let optionBars: Int
        let status: String      // ok | warn | fail | none
        let issues: [Issue]
    }
    struct Counts: Codable, Equatable { let ok: Int; let warn: Int; let fail: Int }
    let underlying: String
    let month: String
    let days: [Day]
    let summary: Counts
}

struct ExpiryInfo: Codable, Equatable, Identifiable {
    var id: String { expiry }
    let expiry: String
    let contracts: Int
    let from: String
    let to: String
    let days: Int
}

struct OptionChain: Codable, Equatable {
    struct Side: Codable, Equatable { let ltp: Double?; let volume: Int64?; let oi: Int64?; let asOf: String }
    struct Row: Codable, Equatable, Identifiable {
        var id: Double { strike }
        let strike: Double
        let CE: Side?
        let PE: Side?
    }
    let underlying: String
    let expiry: String
    let date: String
    let at: String
    let spot: Double?
    let strikes: [Row]
}

struct Bar: Codable, Equatable, Identifiable {
    var id: String { ts }
    let ts: String
    let o: Double
    let h: Double
    let l: Double
    let c: Double
    let v: Int64?
    let oi: Int64?

    /// Store timestamps are naive IST ("2026-10-15 09:15:00").
    var date: Date? { Fmt.parseIST(ts) }
}

struct RecorderInfo: Codable, Equatable {
    struct Status: Codable, Equatable {
        let state: String?          // idle | waiting | recording | finished
        let note: String?
        let day: String?
        let instruments: Int?
        let ticks: Int?
        let lastTick: Date?
        let connected: Bool?
        let reconnects: Int?
        let gaps: [String]?
        let stale: Bool?
        let seconds: Bool?
        let updatedAt: Date?
    }
    struct Settings: Codable, Equatable {
        var underlyings: [String]
        var expiries: Int
        var strikesEachSide: Int
        var recordSeconds: Bool
        var keepSecondsDays: Int
    }
    struct Run: Codable, Equatable, Identifiable {
        var id: String { day }
        let day: String
        let status: String
        let reason: String
        let attempts: Int
    }
    let status: Status
    let settings: Settings
    let runs: [Run]
}

// MARK: research (phase 2)

struct StrategyTemplate: Codable, Identifiable, Hashable {
    struct Stop: Codable, Hashable { let on: String?; let type: String?; let value: Double? }
    let id: String
    let name: String
    let underlying: String
    let kind: String
    let notes: String
    let stopLoss: Stop?
    let maxLossPerDay: Double
    let capital: Double
}

struct Headline: Codable, Equatable {
    let netPnl: Double?
    let grossPnl: Double?
    let charges: Double?
    let chargesPctOfGross: Double?
    let trades: Int?
    let winRatePct: Double?
    let maxDrawdown: Double?
    let expectancy: Double?
    let returnPct: Double?
    let profitFactor: Double?
    let luckWarning: Bool?
    let realPriceFillsPct: Double?
    let byYear: [String: Double]?
}

struct BacktestRunItem: Codable, Identifiable, Equatable, Hashable {
    static func == (a: Self, b: Self) -> Bool { a.id == b.id && a.status == b.status && a.progress == b.progress && a.starred == b.starred }
    func hash(into h: inout Hasher) { h.combine(id) }
    let id: Int
    let name: String
    let template: String?
    let underlying: String
    let start: String
    let end: String
    let status: String
    let progress: Int
    let note: String
    let summary: Headline?
    let starred: Bool
    let archived: Bool
    let fillModel: String
    let createdAt: Date
    let finishedAt: Date?
    var kind: String? = "backtest"
    var costStress: Double? = 1
    var strategyId: Int? = nil
}

struct Metrics: Codable, Equatable {
    struct DayPnl: Codable, Equatable { let date: String?; let pnl: Double? }
    struct WeekPnl: Codable, Equatable { let week: String; let pnl: Double }
    struct ReasonStat: Codable, Equatable { let trades: Int; let net: Double }
    struct MonteCarlo: Codable, Equatable { let ddP95: Double; let ddP50: Double; let pLosingBlock: Double; let blockTrades: Int }
    let netPnl: Double
    let grossPnl: Double
    let charges: Double
    let chargesPctOfGross: Double?
    let returnPct: Double?
    let trades: Int
    let winRatePct: Double?
    let avgWin: Double?
    let avgLoss: Double?
    let expectancy: Double?
    let maxDrawdown: Double
    let maxDrawdownPct: Double?
    let longestDrawdownDays: Int?
    let worstDay: DayPnl?
    let bestTrade: Double?
    let worstTrade: Double?
    let top5SharePct: Double?
    let byYear: [String: Double]
    let days: Int
    let daysTraded: Int?
    let realPriceFillsPct: Double?
    let openAtEnd: Int?
    let netOnEventDays: Double?
    let netExcludingEventDays: Double?
    let eventDaysCount: Int?
    let peakMargin: Double?
    let avgMarginWhenOpen: Double?
    let returnOnPeakMarginPct: Double?
    let netWithoutWorstDay: Double?
    let sortino: Double?
    let calmar: Double?
    let byMonth: [String: Double]?
    let byWeekday: [String: Double]?
    let byExitReason: [String: ReasonStat]?
    let monthsProfitablePct: Double?
    let netWithChargesX15: Double?
    let monteCarlo: MonteCarlo?
    let indexMovePct: Double?
    let daysOverCapitalMargin: Int?
    let fillsOver10pctVolume: Int?
    let scoreSteady: Double?
    let profitFactor: Double?
    let returnPerYearPct: Double?
    let worstWeek: WeekPnl?
    let bestWeek: WeekPnl?
    let avgDaysInTrade: Double?
    let maxDaysInTrade: Double?
    let luckWarning: Bool?
}

struct RunAudit: Codable, Equatable {
    struct Dates: Codable, Equatable { let asked: [String]; let withData: Int; let first: String?; let last: String? }
    struct Fills: Codable, Equatable { let total: Int; let stale: Int; let realPricePct: Double?; let over10pctOfVolume: Int? }
    struct FillModel: Codable, Equatable {
        let fillModel: String?; let slippagePct: Double?; let fillMaxAgeMin: Int?; let costStress: Double?
        let slOrder: String?; let stopFillFrac: Double?; let minHalfSpread: [[Double]]?
    }
    let dates: Dates
    let fills: Fills
    let fillModel: FillModel
    let lotSizesByExpiry: [String: Int?]
    let chargesRates: [String: [String: Double]]
    let skipped: [String: Int]
    let exitsAtOldPrice: Int?
    let engineVersion: String
    let specHash: String
}

struct BacktestDetail: Decodable {
    let run: BacktestRunItem
    let metrics: Metrics?
    let audit: RunAudit?
    private enum K: String, CodingKey { case metrics, audit }
    init(from decoder: Decoder) throws {
        run = try BacktestRunItem(from: decoder)
        let c = try decoder.container(keyedBy: K.self)
        metrics = try c.decodeIfPresent(Metrics.self, forKey: .metrics)
        audit = try c.decodeIfPresent(RunAudit.self, forKey: .audit)
    }
}

struct CurveDay: Codable, Equatable, Identifiable {
    var id: String { date }
    let date: String
    let pnl: Double
    let equity: Double
    let drawdown: Double
}
struct Curves: Codable { let days: [CurveDay] }

struct FillItem: Codable, Equatable, Hashable {
    let ts: String; let symbol: String; let side: String; let qty: Int; let price: Double
    let charges: Double; let reason: String; let stale: Bool
}
struct TradeItem: Codable, Identifiable, Equatable, Hashable {
    let id: Int
    let entryTs: String
    let exitTs: String
    let exitReason: String
    let gross: Double
    let charges: Double
    let net: Double
    let fills: [FillItem]
}
struct TradePage: Codable { let total: Int; let trades: [TradeItem] }

// MARK: market strip, health, version

struct IndexQuote: Codable, Identifiable, Equatable {
    var id: String { underlying }
    let underlying: String
    let price: Double
    let asOf: Date
    let live: Bool
    let prevClose: Double?
    let changePct: Double?
}
struct IndexStrip: Codable, Equatable {
    struct Feed: Codable, Equatable { let live: Bool; let lastTick: Date?; let stale: Bool? }
    let indexes: [IndexQuote]
    let feed: Feed
}

struct HealthInfo: Codable, Equatable {
    struct Check: Codable, Equatable, Identifiable {
        var id: String { name }
        let name: String
        let ok: Bool?
        let reason: String
        let fails: Int
        let light: String      // green | amber | red | grey
    }
    let checks: [Check]
    let version: String
    let checkedAt: Date?
}

struct VersionInfo: Codable, Equatable {
    struct Available: Codable, Equatable { let current: String; let available: String; let behind: Int; let subject: String }
    struct Update: Codable, Equatable { let state: String; let detail: String; let from: String?; let to: String?; let at: String? }
    let version: String
    let commit: String
    let available: Available?
    let lastUpdate: Update?
    let requested: Bool
}

/// "stopLoss" / "stop_loss" -> "Stop loss" (dictionary keys arrive camel-cased by the decoder).
func humanize(_ key: String) -> String {
    var out = ""
    for ch in key.replacingOccurrences(of: "_", with: " ") {
        if ch.isUppercase, !out.isEmpty, out.last != " " { out.append(" ") }
        out.append(contentsOf: String(ch).lowercased())
    }
    return out.prefix(1).uppercased() + out.dropFirst()
}
