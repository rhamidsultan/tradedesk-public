import ActivityKit
import Foundation

/// Starts, updates and ends the PAPER Live Activity from the session list the app already loads. It runs only
/// while the app is open or recently backgrounded (no push updates yet), and is marked stale after 2 minutes so
/// an old figure is never shown as current (the widget greys it and says it is not updating).
@MainActor
final class PaperActivity {
    static let shared = PaperActivity()
    private var activity: Activity<PaperActivityAttributes>?

    func update(sessions: [JSONValue]) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let live = sessions.filter { $0["live"]?.bool == true && $0["status"]?.string != "stopped" }
        let open = live.reduce(0) { $0 + Int($1["open_legs"]?.number ?? 0) }
        if live.isEmpty {
            end()
            return
        }
        let state = PaperActivityAttributes.ContentState(
            dayPnl: live.reduce(0) { $0 + ($1["day_pnl"]?.number ?? 0) },
            sessions: live.count, openLegs: open,
            nearestStopPct: live.compactMap { $0["nearest_stop_pct"]?.number }.min(),
            asOf: live.compactMap { $0["as_of"]?.string }.max() ?? "—",
            kill: live.contains { !($0["kill"]?.array ?? []).isEmpty })
        let content = ActivityContent(state: state, staleDate: Date.now.addingTimeInterval(120))
        if let a = activity ?? Activity<PaperActivityAttributes>.activities.first {
            activity = a
            Task { await a.update(content) }
        } else {
            activity = try? Activity.request(attributes: PaperActivityAttributes(title: live.count == 1 ? (live[0]["name"]?.string ?? "Paper") : "Paper sessions"),
                                             content: content)
        }
    }

    /// Refresh from the server wherever the app is (Home, foreground), not only on the Trade tab.
    func refresh(_ api: APIClient) async {
        guard let list = try? await api.raw("GET", "/v1/paper/sessions").array else { return }
        update(sessions: list)
    }

    func end() {
        let all = Activity<PaperActivityAttributes>.activities
        activity = nil
        for a in all { Task { await a.end(nil, dismissalPolicy: .immediate) } }
    }
}
