import SwiftUI

/// TradeDesk colours. Meaning is never carried by colour alone: every state also has an icon and words.
extension Color {
    // light / dark pairs chosen for at least 4.5:1 contrast as text
    static let tdGood = adaptive(0x0B7A3B, 0x34C759)
    static let tdBad = adaptive(0xC62828, 0xFF6B5E)
    static let tdWarn = adaptive(0xA15C00, 0xFFB340)
    static let tdPaper = adaptive(0x2F4FD8, 0x8EA4FF)   // paper trading is always blue
    static let tdLive = adaptive(0xB0128A, 0xFF6FD8)    // live is magenta, so it never reads as an error

    private static func adaptive(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(UIColor { $0.userInterfaceStyle == .dark ? UIColor(hex: dark) : UIColor(hex: light) })
    }
}

private extension UIColor {
    convenience init(hex: UInt32) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }
}

enum Level {
    static func color(_ level: String) -> Color {
        switch level { case "critical": .tdBad; case "important": .tdWarn; default: .secondary }
    }
    static func icon(_ level: String) -> String {
        switch level { case "critical": "exclamationmark.octagon.fill"; case "important": "exclamationmark.triangle.fill"
        default: "info.circle" }
    }
}

/// Soft gradient behind glass so the material has something to refract.
struct AppBackground: View {
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        LinearGradient(colors: scheme == .dark
                       ? [Color(red: 0.05, green: 0.06, blue: 0.10), Color(red: 0.08, green: 0.10, blue: 0.18)]
                       : [Color(red: 0.93, green: 0.95, blue: 1.0), Color(red: 0.98, green: 0.97, blue: 0.95)],
                       startPoint: .top, endPoint: .bottom)
            .ignoresSafeArea()
    }
}
