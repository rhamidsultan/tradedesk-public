import SwiftUI

/// One index: a month calendar where every day shows whether its data is complete, plus the expiry timeline.
struct DataIndexView: View {
    @Environment(AppModel.self) private var model
    let underlying: String
    @State private var month: Date = .now
    @State private var data: DataMonth?
    @State private var expiries: [ExpiryInfo] = []
    @State private var error: String?
    @State private var tab = 0

    var body: some View {
        List {
            Picker("View", selection: $tab) {
                Text("Days").tag(0)
                Text("Expiries").tag(1)
            }
            .pickerStyle(.segmented)
            .listRowBackground(Color.clear)

            if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            if tab == 0 { calendarSection } else { expirySection }
        }
        .navigationTitle(underlying)
        .task(id: monthKey) { await loadMonth() }
        .task { await loadExpiries() }
    }

    private var monthKey: String {
        let f = DateFormatter(); f.timeZone = Fmt.ist; f.dateFormat = "yyyy-MM"
        return f.string(from: month)
    }

    @ViewBuilder private var calendarSection: some View {
        Section {
            HStack {
                Button { shift(-1) } label: { Image(systemName: "chevron.left") }
                    .accessibilityLabel("Previous month")
                Spacer()
                Text(monthTitle).font(.headline)
                Spacer()
                Button { shift(1) } label: { Image(systemName: "chevron.right") }
                    .accessibilityLabel("Next month")
            }
            .buttonStyle(.borderless)
            if let d = data {
                MonthGrid(days: d.days, underlying: underlying)
                HStack(spacing: 12) {
                    legend("Complete", .tdGood, "checkmark.circle.fill", d.summary.ok)
                    legend("Check", .tdWarn, "exclamationmark.circle.fill", d.summary.warn)
                    legend("Missing", .tdBad, "xmark.circle.fill", d.summary.fail)
                }
                .font(.caption)
            } else if error == nil {
                ProgressView()
            }
        }
        if let d = data {
            let problems = d.days.filter { !$0.issues.isEmpty }
            if !problems.isEmpty {
                Section("Days to look at") {
                    ForEach(problems) { day in
                        NavigationLink(value: Route.dataDay(underlying, day.date)) {
                            VStack(alignment: .leading) {
                                Text(Fmt.marketDay(day.date)).font(.subheadline.weight(.semibold))
                                ForEach(day.issues, id: \.self) { i in
                                    Label(i.text, systemImage: levelIcon(i.level)).font(.caption).foregroundStyle(levelColor(i.level))
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private var expirySection: some View {
        let grouped = Dictionary(grouping: expiries) { String($0.expiry.prefix(7)) }
        ForEach(grouped.keys.sorted(by: >), id: \.self) { m in
            Section(Fmt.marketDay(m + "-01").split(separator: " ").dropFirst().joined(separator: " ")) {
                ForEach(grouped[m] ?? []) { e in
                    NavigationLink(value: Route.chain(underlying, e.expiry, e.to)) {
                        LabeledContent {
                            Text("\(e.contracts) contracts · \(e.days) days").monospacedDigit()
                        } label: {
                            Text(Fmt.marketDay(e.expiry))
                        }
                    }
                }
            }
        }
        if expiries.isEmpty && error == nil { Text("No option prices stored for \(underlying).").foregroundStyle(.secondary) }
    }

    private var monthTitle: String {
        let f = DateFormatter(); f.timeZone = Fmt.ist; f.dateFormat = "MMMM yyyy"
        return f.string(from: month)
    }

    private func shift(_ n: Int) {
        var c = Calendar(identifier: .gregorian); c.timeZone = Fmt.ist
        month = c.date(byAdding: .month, value: n, to: month) ?? month
    }

    private func legend(_ t: String, _ c: Color, _ i: String, _ n: Int) -> some View {
        Label("\(t) \(n)", systemImage: i).foregroundStyle(c).monospacedDigit()
    }

    private func loadMonth() async {
        data = nil
        do { data = try await model.api.get("/v1/data/\(underlying)/month?m=\(monthKey)"); error = nil }
        catch { self.error = error.localizedDescription }
    }

    private func loadExpiries() async {
        do { expiries = try await model.api.get("/v1/data/\(underlying)/expiries") }
        catch { self.error = error.localizedDescription }
    }
}

func levelIcon(_ level: String) -> String {
    switch level { case "fail": "xmark.circle.fill"; case "warn": "exclamationmark.circle.fill"; default: "info.circle" }
}

func levelColor(_ level: String) -> Color {
    switch level { case "fail": .tdBad; case "warn": .tdWarn; default: .secondary }
}

/// 7-column month grid, Monday first. Each day: number + a status symbol (never colour alone).
private struct MonthGrid: View {
    @Environment(AppModel.self) private var model
    let days: [DataMonth.Day]
    let underlying: String

    var body: some View {
        let lead = leadingBlanks
        let cols = Array(repeating: GridItem(.flexible(), spacing: 4), count: 7)
        LazyVGrid(columns: cols, spacing: 6) {
            ForEach(["M", "T", "W", "T", "F", "S", "S"].indices, id: \.self) { i in
                Text(["M", "T", "W", "T", "F", "S", "S"][i]).font(.caption2).foregroundStyle(.secondary)
            }
            ForEach(0..<lead, id: \.self) { _ in Color.clear.frame(height: 40) }
            ForEach(days) { d in
                Button { model.morePath.append(.dataDay(underlying, d.date)) } label: { cell(d) }
                    .buttonStyle(.borderless)
                    .disabled(d.status == "none")
            }
        }
    }

    private var leadingBlanks: Int {
        guard let first = days.first, let d = Fmt.parseIST(first.date) else { return 0 }
        var c = Calendar(identifier: .gregorian); c.timeZone = Fmt.ist
        return (c.component(.weekday, from: d) + 5) % 7   // Monday = 0
    }

    private func cell(_ d: DataMonth.Day) -> some View {
        let n = Int(d.date.suffix(2)) ?? 0
        let (sym, col): (String?, Color) = switch d.status {
        case "ok": ("checkmark", .tdGood)
        case "warn": ("exclamationmark", .tdWarn)
        case "fail": ("xmark", .tdBad)
        default: (nil, .secondary)
        }
        return VStack(spacing: 2) {
            Text("\(n)").font(.callout.monospacedDigit()).foregroundStyle(d.tradingDay ? .primary : .secondary)
            if let sym { Image(systemName: sym).font(.caption2.weight(.bold)).foregroundStyle(col) }
            else { Text(" ").font(.caption2) }
        }
        .frame(maxWidth: .infinity, minHeight: 44)
        .background(col.opacity(d.status == "none" ? 0 : 0.12), in: .rect(cornerRadius: 8))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(Fmt.marketDay(d.date)): \(d.status == "none" ? "no market" : d.status == "ok" ? "complete" : d.status == "warn" ? "check" : "missing")")
    }
}
