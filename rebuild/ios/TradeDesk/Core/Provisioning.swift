import Foundation
import UserNotifications

/// The free Apple account signs the app for 7 days. We read the expiry from the embedded profile so the app
/// can show "re-install in N days", tell the server (for the Saturday reminder) and post a local reminder.
enum Provisioning {
    static func expiryDate(bundle: Bundle = .main) -> Date? {
        guard let url = bundle.url(forResource: "embedded", withExtension: "mobileprovision"),
              let data = try? Data(contentsOf: url) else { return nil }   // nil in the simulator and App Store builds
        return expiryDate(fromProfile: data)
    }

    /// The profile is a signed CMS blob with a plain XML plist inside.
    static func expiryDate(fromProfile data: Data) -> Date? {
        guard let text = String(data: data, encoding: .isoLatin1),
              let start = text.range(of: "<?xml"), let end = text.range(of: "</plist>") else { return nil }
        let xml = Data(text[start.lowerBound..<end.upperBound].utf8)
        let plist = try? PropertyListSerialization.propertyList(from: xml, format: nil) as? [String: Any]
        return plist?["ExpirationDate"] as? Date
    }

    static func daysLeft(until expiry: Date, now: Date = .now) -> Int {
        max(0, Int(expiry.timeIntervalSince(now) / 86_400))
    }

    /// "5 days", or "14 h" on the last day, so it never says 0 while the app still works.
    static func timeLeft(until expiry: Date, now: Date = .now) -> String {
        let s = expiry.timeIntervalSince(now)
        if s <= 0 { return "Expired" }
        if s < 86_400 { return "\(max(1, Int(s / 3600))) h" }
        let d = Int(s / 86_400)
        return d == 1 ? "1 day" : "\(d) days"
    }

    /// One local notification a day before expiry, at 10:00 the phone's local time.
    static func scheduleReminder(expiry: Date) async {
        let center = UNUserNotificationCenter.current()
        _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
        center.removePendingNotificationRequests(withIdentifiers: ["reinstall"])
        guard let dayBefore = Calendar.current.date(byAdding: .day, value: -1, to: expiry) else { return }
        var at = Calendar.current.dateComponents([.year, .month, .day], from: dayBefore)
        at.hour = 10
        guard let fire = Calendar.current.date(from: at), fire > .now else { return }
        let content = UNMutableNotificationContent()
        content.title = "Re-install TradeDesk"
        content.body = "This install stops tomorrow. Open Xcode on the Mac and press Run with the phone on the same Wi-Fi."
        content.sound = .default
        let trigger = UNCalendarNotificationTrigger(dateMatching: at, repeats: false)
        try? await center.add(UNNotificationRequest(identifier: "reinstall", content: content, trigger: trigger))
    }
}
