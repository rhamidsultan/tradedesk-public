import SwiftUI

@main
struct TradeDeskApp: App {
    @State private var model = AppModel()
    @AppStorage("theme") private var theme = "system"

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .preferredColorScheme(theme == "light" ? .light : theme == "dark" ? .dark : nil)
                .task { await model.start() }
                .onOpenURL { model.open($0) }
        }
    }
}

struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Group {
            switch model.phase {
            case .loading: ProgressView().controlSize(.large)
            case .onboarding: OnboardingView()
            case .locked: LockView()
            case .ready: MainTabs()
            }
        }
        .animation(.smooth, value: model.phase)
    }
}
