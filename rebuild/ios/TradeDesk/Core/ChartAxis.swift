import Charts
import SwiftUI

extension View {
    /// Time axis for intraday charts, labelled in IST whatever the phone's time zone (a chart's own labels follow
    /// the phone: in Qatar 09:15 IST read "6:45 AM").
    func istTimeAxis() -> some View {
        chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 5)) { v in
                AxisGridLine()
                AxisTick()
                AxisValueLabel {
                    if let d = v.as(Date.self) { Text(istClock.string(from: d)) }
                }
            }
        }
    }
}

private let istClock: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = Fmt.ist
    f.dateFormat = "HH:mm"
    return f
}()
