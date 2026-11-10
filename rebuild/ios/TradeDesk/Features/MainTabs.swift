import SwiftUI

/// Five tabs as in the approved mockups: Home, Strategies, Trade, Orders, More.
/// The glass status strip rides above the tab bar (bottom accessory) and the bar shrinks while scrolling.
struct MainTabs: View {
    @Environment(AppModel.self) private var model
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        @Bindable var model = model
        TabView(selection: $model.tab) {
            Tab("Home", systemImage: "house", value: AppTab.home) { HomeView() }
            if model.can("research") || model.can("backtests") {
                Tab("Strategies", systemImage: "square.stack.3d.up", value: AppTab.strategies) { StrategiesView() }
            }
            if model.can("paper") || model.can("research") {
                Tab("Trade", systemImage: "waveform.path.ecg", value: AppTab.trade) { TradeView() }
            }
            if model.can("live") {
                Tab("Orders", systemImage: "list.bullet.rectangle", value: AppTab.orders) { OrdersView() }
                    .badge(model.status?.live.openLegs ?? 0)
            }
            Tab("More", systemImage: "ellipsis.circle", value: AppTab.more) { MoreView() }
        }
        .tabBarMinimizeBehavior(.onScrollDown)
        .tabViewBottomAccessory { StatusStrip() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task {
                    try? await model.refresh()
                    await PaperActivity.shared.refresh(model.api)
                }
            }
        }
    }
}
