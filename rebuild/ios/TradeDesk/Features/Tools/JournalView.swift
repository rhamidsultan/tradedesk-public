import SwiftUI

/// More › Journal: every closed paper, shadow and live trade (imported by the server), with the trader's setup tag,
/// tags, notes and rating, and net results by setup. Numbers are net of charges.
struct JournalView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    @State private var source = "all"
    @State private var setup = "all"
    @State private var data: JSONValue?
    @State private var error: String?
    @State private var editing: JSONValue?

    var body: some View {
        List {
            Section {
                Picker("Trades", selection: $source) {
                    Text("All").tag("all"); Text("Paper").tag("paper"); Text("Shadow").tag("shadow"); Text("Live").tag("live")
                }
                .pickerStyle(.segmented)
                Picker("Setup", selection: $setup) {
                    Text("Any setup").tag("all")
                    Text("Untagged").tag("untagged")
                    ForEach(setups, id: \.self) { Text($0.capitalized).tag($0) }
                }
            }
            if let error {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn)
                    Button("Retry") { Task { await load() } }
                }
            }
            if source == "all", let by = data?["stats_by_source"]?.object {
                // never add paper, shadow and live money together
                ForEach(["live", "shadow", "paper", "manual"].filter { by[$0] != nil }, id: \.self) { src in
                    statsSection(by[src]!, source: src)
                }
            } else if let s = data?["stats"] {
                statsSection(s, source: source)
            }
            let entries = data?["entries"]?.array ?? []
            Section("Trades") {
                if data == nil && error == nil {
                    ProgressView().frame(maxWidth: .infinity)
                }
                if data != nil && entries.isEmpty {
                    Text("No closed trades yet. Trades from paper, shadow and live sessions appear here when they close.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                ForEach(Array(entries.enumerated()), id: \.offset) { _, e in
                    Button { editing = e } label: { entryRow(e) }.buttonStyle(.plain)
                }
            }
        }
        .navigationTitle("Journal")
        .refreshable { await load() }
        .task(id: "\(source)|\(setup)") { await load() }
        .sheet(item: Binding(get: { editing.map(Box.init) }, set: { editing = $0?.v })) { b in
            JournalEntryEditor(entry: b.v, setups: setups) { updated in
                editing = nil
                if updated { Task { await load() } }
            }
        }
    }

    private func when(_ s: String?) -> String {
        guard let s, s.count >= 16 else { return s ?? "" }
        return "\(Fmt.marketDay(String(s.prefix(10)))), \(s.suffix(5))"
    }

    private var setups: [String] { (data?["setups"]?.array ?? []).compactMap(\.string) }

    private func money(_ v: Double?, signed: Bool = true) -> String {
        guard let v else { return "—" }
        return Fmt.rupees(v, hidden: hide, signed: signed)
    }

    private func statsSection(_ s: JSONValue, source: String) -> some View {
        let a = s["all"]
        return Section {
            HStack { sourcePill(source); Spacer() }
            PnlLine(label: "Net, \(Int(a?["trades"]?.number ?? 0)) trades", value: a?["net"]?.number ?? 0, big: true,
                    mode: source.capitalized)
            HStack {
                stat("Win rate", pct(a?["win_rate"]?.number))
                stat("Avg win", money(a?["avg_win"]?.number))
                stat("Avg loss", money(a?["avg_loss"]?.number))
                stat("Per trade", money(a?["expectancy"]?.number))
            }
            ForEach(Array((s["by_setup"]?.array ?? []).enumerated()), id: \.offset) { _, b in
                HStack {
                    Text((b["setup"]?.string ?? "").capitalized)
                    Spacer()
                    Text("\(Int(b["trades"]?.number ?? 0)) · \(pct(b["win_rate"]?.number)) won").font(.caption).foregroundStyle(.secondary)
                    Text(money(b["net"]?.number)).monospacedDigit()
                        .foregroundStyle((b["net"]?.number ?? 0) >= 0 ? Color.tdGood : Color.tdBad)
                }
                .font(.subheadline)
            }
        } header: { Text("\(source.capitalized) results (net of charges)") }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(spacing: 2) {
            Text(value).font(.subheadline.weight(.semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }

    private func entryRow(_ e: JSONValue) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                sourcePill(e["source"]?.string ?? "")
                Text(e["name"]?.string ?? "").font(.headline).lineLimit(1)
                Spacer()
                Text(money(e["net"]?.number)).monospacedDigit()
                    .foregroundStyle((e["net"]?.number ?? 0) >= 0 ? Color.tdGood : Color.tdBad)
            }
            HStack(spacing: 6) {
                Text("\(when(e["entry_at"]?.string)) → \(String((e["exit_at"]?.string ?? "").suffix(5))) IST")
                if let r = e["exit_reason"]?.string { Text("· \(r.replacingOccurrences(of: "_", with: " "))") }
            }
            .font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 6) {
                if let s = e["setup"]?.string { Text(s.capitalized).font(.caption.weight(.semibold)).foregroundStyle(.tint) }
                ForEach((e["tags"]?.array ?? []).compactMap(\.string), id: \.self) { Text("#\($0)").font(.caption).foregroundStyle(.secondary) }
                if let r = e["rating"]?.number { Text(String(repeating: "★", count: Int(r))).font(.caption).foregroundStyle(Color.tdWarn) }
                if !(e["notes"]?.string ?? "").isEmpty { Image(systemName: "note.text").font(.caption).foregroundStyle(.secondary) }
            }
        }
        .padding(.vertical, 2)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Edit setup, tags and notes")
    }

    @ViewBuilder func sourcePill(_ s: String) -> some View {
        JournalSourcePill(source: s)
    }
}

struct JournalSourcePill: View {
    let source: String
    var body: some View {
        switch source {
        case "live": StatusPill(text: "LIVE", systemImage: "bolt.fill", color: .tdLive)
        case "shadow": StatusPill(text: "SHADOW", systemImage: "eye", color: .secondary)
        case "paper": StatusPill(text: "PAPER", systemImage: "doc.text", color: .tdPaper)
        case "all": EmptyView()
        default: StatusPill(text: source.uppercased(), systemImage: "pencil", color: .secondary)
        }
    }
}

extension JournalView {
    private func load() async {
        var q = "/v1/journal?"
        if source != "all" { q += "source=\(source)&" }
        if setup != "all" { q += "setup=\(setup.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? setup)" }
        do { data = try await model.api.raw("GET", q); error = nil }
        catch { self.error = "Couldn't load the journal: \(error.localizedDescription)" }
    }
}

private struct Box: Identifiable { let v: JSONValue; var id: Int { Int(v["id"]?.number ?? 0) } }

/// Setup, tags, notes and rating for one trade; the numbers stay as imported.
struct JournalEntryEditor: View {
    @Environment(AppModel.self) private var model
    @AppStorage("hideAmounts") private var hide = false
    let entry: JSONValue
    let setups: [String]
    var done: (Bool) -> Void
    @State private var setup = ""
    @State private var tags = ""
    @State private var notes = ""
    @State private var rating = 0
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent("Net", value: Fmt.rupees(entry["net"]?.number ?? 0, hidden: hide, signed: true))
                    LabeledContent("Gross", value: Fmt.rupees(entry["gross"]?.number ?? 0, hidden: hide, signed: true))
                    LabeledContent("Charges", value: Fmt.rupees(entry["charges"]?.number ?? 0, hidden: hide))
                    ForEach(Array((entry["legs"]?.array ?? []).enumerated()), id: \.offset) { _, l in
                        Text("\(l["side"]?.string == "SELL" ? "Sell" : "Buy") \(Fmt.contract(l["symbol"]?.string ?? "")) × \(Int(l["qty"]?.number ?? 0)) qty @ \(num(l["entry"]?.number))"
                             + (l["exit"]?.number.map { " → \(num($0))" } ?? ""))
                            .font(.caption)
                    }
                } header: {
                    HStack {
                        JournalSourcePill(source: entry["source"]?.string ?? "")
                        Text("\(entry["name"]?.string ?? "") · \(entry["entry_at"]?.string ?? "") IST")
                    }
                }
                Section("Your notes") {
                    Picker("Setup", selection: $setup) {
                        Text("None").tag("")
                        ForEach(setups, id: \.self) { Text($0.capitalized).tag($0) }
                    }
                    TextField("Tags, separated by commas", text: $tags).textInputAutocapitalization(.never)
                    HStack {
                        Text("Rating")
                        Spacer()
                        ForEach(1...5, id: \.self) { i in
                            Button { rating = rating == i ? 0 : i } label: {
                                Image(systemName: i <= rating ? "star.fill" : "star").foregroundStyle(Color.tdWarn)
                                    .frame(minWidth: 44, minHeight: 44)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("\(i) star\(i == 1 ? "" : "s")")
                            .accessibilityAddTraits(i == rating ? .isSelected : [])
                        }
                    }
                    TextField("What went right or wrong?", text: $notes, axis: .vertical).lineLimit(3...10)
                }
                if let error { Text(error).foregroundStyle(Color.tdBad) }
            }
            .navigationTitle("Trade notes")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { done(false) } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { Task { await save() } }.disabled(saving) }
            }
            .onAppear {
                setup = entry["setup"]?.string ?? ""
                tags = (entry["tags"]?.array ?? []).compactMap(\.string).joined(separator: ", ")
                notes = entry["notes"]?.string ?? ""
                rating = Int(entry["rating"]?.number ?? 0)
            }
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        let t = tags.split(separator: ",").map { JSONValue.string($0.trimmingCharacters(in: .whitespaces)) }
        let body: JSONValue = .object(["setup": setup.isEmpty ? .null : .string(setup), "tags": .array(t),
                                       "notes": .string(notes), "rating": .number(Double(rating))])
        do {
            _ = try await model.api.raw("PATCH", "/v1/journal/\(Int(entry["id"]?.number ?? 0))", body: body)
            done(true)
        } catch { self.error = error.localizedDescription }
    }
}
