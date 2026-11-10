import SwiftUI

/// More › Users and access (owner): family and friend logins, invites with a preset, access per area, Hide amounts,
/// switching a login off, removing its Face ID keys, and what each did.
struct UsersView: View {
    @Environment(AppModel.self) private var model
    @State private var data: JSONValue?
    @State private var error: String?
    @State private var inviting = false

    var body: some View {
        List {
            if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Color.tdWarn) }
            let users = data?["users"]?.array ?? []
            Section {
                ForEach(Array(users.enumerated()), id: \.offset) { _, u in
                    if u["role"]?.string == "owner" {
                        row(u)
                    } else {
                        NavigationLink(value: Route.userDetail(Int(u["id"]?.number ?? 0))) { row(u) }
                    }
                }
                Button("Invite someone", systemImage: "person.badge.plus") { inviting = true }
                    .frame(minHeight: 44)
            } footer: {
                Text("Each login has its own Face ID key and, to trade, its own Zerodha account. The portfolio pages show your holdings; give them only to family you trust, with amounts hidden if you like.")
            }
            if data == nil && error == nil { ProgressView().frame(maxWidth: .infinity) }
        }
        .navigationTitle("Users and access")
        .refreshable { await load() }
        .task { await load() }
        .sheet(isPresented: $inviting) {
            InviteSheet(presets: data?["presets"]?.array ?? [], areas: data?["areas"]?.array ?? []) {
                inviting = false
                Task { await load() }
            }
        }
    }

    private func row(_ u: JSONValue) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(u["name"]?.string ?? "").font(.headline)
                Spacer()
                if u["disabled"]?.bool == true {
                    StatusPill(text: "Switched off", systemImage: "slash.circle", color: .tdBad)
                } else {
                    StatusPill(text: (u["role"]?.string ?? "").capitalized, systemImage: u["role"]?.string == "owner" ? "crown" : "person",
                               color: .secondary)
                }
            }
            Text(summary(u)).font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                let devs = (u["devices"]?.array ?? []).filter { $0["revoked"]?.bool != true }.count
                Text("\(devs) device\(devs == 1 ? "" : "s")")
                if let s = u["last_seen"]?.string, let d = ServerDate.parse(s) { Text("· last seen \(Fmt.dayTime(d))") }
                if u["hide_amounts"]?.bool == true { Label("amounts hidden", systemImage: "eye.slash") }
            }
            .font(.caption2).foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private func summary(_ u: JSONValue) -> String {
        if u["role"]?.string == "owner" { return "Everything" }
        if let p = u["preset"]?.string, let label = (data?["presets"]?.array ?? []).first(where: { $0["key"]?.string == p })?["label"]?.string {
            return label
        }
        let acc = u["access"]?.object ?? [:]
        let areas = data?["areas"]?.array ?? []
        let parts = areas.compactMap { a -> String? in
            guard let k = a["key"]?.string, let lvl = acc[k]?.string, lvl != "none" else { return nil }
            return "\(a["label"]?.string ?? k) (\(lvl))"
        }
        return parts.isEmpty ? "No access" : parts.joined(separator: ", ")
    }

    private func load() async {
        do { data = try await model.api.raw("GET", "/v1/users"); error = nil }
        catch { self.error = error.localizedDescription }
    }
}

/// Invite a family member or friend: a preset (or own choice), then a one-time code to type in their app.
struct InviteSheet: View {
    @Environment(AppModel.self) private var model
    let presets: [JSONValue]
    let areas: [JSONValue]
    var done: () -> Void
    @State private var name = ""
    @State private var preset = "family"
    @State private var code: String?
    @State private var days = 3
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                if let code {
                    Section {
                        Text(code).font(.title.monospaced().weight(.semibold)).textSelection(.enabled)
                            .frame(maxWidth: .infinity)
                        ShareLink(item: "Your TradeDesk invite code: \(code) (valid \(days) days, one use). Install the app, open it and enter this code.") {
                            Label("Send the code", systemImage: "square.and.arrow.up").frame(maxWidth: .infinity, minHeight: 44)
                        }
                    } footer: { Text("Valid for \(days) days and one device. They enter it in the app's first screen.") }
                } else {
                    Section { TextField("Name", text: $name).textContentType(.name) }
                    Section {
                        Picker("Starts with", selection: $preset) {
                            ForEach(presets.compactMap { $0["key"]?.string }, id: \.self) { k in
                                Text(presets.first { $0["key"]?.string == k }?["label"]?.string ?? k).tag(k)
                            }
                        }
                    } footer: {
                        Text(presetNote)
                    }
                    if let error { Text(error).foregroundStyle(Color.tdBad) }
                }
            }
            .navigationTitle(code == nil ? "Invite" : "Invite code")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(code == nil ? "Cancel" : "Done") { done() } }
                if code == nil {
                    ToolbarItem(placement: .confirmationAction) {
                        Button(busy ? "Creating…" : "Create") { Task { await create() } }
                            .disabled(busy || name.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            }
        }
    }

    private var presetNote: String {
        guard let p = presets.first(where: { $0["key"]?.string == preset }) else { return "" }
        let acc = p["access"]?.object ?? [:]
        let parts = areas.compactMap { a -> String? in
            guard let k = a["key"]?.string, let lvl = acc[k]?.string, lvl != "none" else { return nil }
            return "\(a["label"]?.string ?? k): \(lvl)"
        }
        return parts.joined(separator: " · ") + (p["hide_amounts"]?.bool == true ? " · amounts hidden" : "") +
            ". You can change any of it later."
    }

    private func create() async {
        busy = true
        defer { busy = false }
        do {
            let r = try await model.api.raw("POST", "/v1/users/invite", body: JSONValue.object([
                "name": .string(name.trimmingCharacters(in: .whitespaces)), "preset": .string(preset)]))
            code = r["code"]?.string
            days = Int(r["expires_in_days"]?.number ?? 3)
        } catch { self.error = error.localizedDescription }
    }
}

/// One login: role, access per area, Hide amounts, switch off, remove Face ID keys, activity.
struct UserDetailView: View {
    @Environment(AppModel.self) private var model
    let userID: Int
    @State private var all: JSONValue?
    @State private var access: [String: String] = [:]
    @State private var hide = false
    @State private var error: String?
    @State private var note: String?
    @State private var confirm: String?          // disable | remove-keys
    @State private var activity: [JSONValue] = []

    private var user: JSONValue? { (all?["users"]?.array ?? []).first { Int($0["id"]?.number ?? -1) == userID } }

    var body: some View {
        Form {
            if let error { Text(error).foregroundStyle(Color.tdBad) }
            if let note { Label(note, systemImage: "checkmark.circle").foregroundStyle(Color.tdGood) }
            if let u = user {
                Section {
                    Toggle("Hide amounts", isOn: Binding(get: { hide }, set: { hide = $0; Task { await save() } }))
                } footer: { Text("Hide amounts: they see percentages only, never rupees, quantities or prices. Changes save at once.") }
                Section {
                    ForEach(all?["areas"]?.array ?? [], id: \.self) { a in areaRow(a) }
                } header: { Text("Access") } footer: {
                    Text("View: can look. Use: can also change things there (it makes them a trader). Trading always uses their own Zerodha account. Lowering live trading stops new entries; they can always press Stop all.")
                }
                Section {
                    ForEach(all?["presets"]?.array ?? [], id: \.self) { p in
                        Button("Set to \(p["label"]?.string ?? "")") {
                            access = (p["access"]?.object ?? [:]).compactMapValues(\.string)
                            hide = p["hide_amounts"]?.bool == true
                            Task { await save() }
                        }
                    }
                }
                Section {
                    if u["disabled"]?.bool == true {
                        Button("Switch this login back on", systemImage: "power") { Task { await post("enable") } }
                    } else {
                        Button("Switch this login off", systemImage: "slash.circle", role: .destructive) { confirm = "disable" }
                    }
                    Button("Remove Face ID keys", systemImage: "key.slash", role: .destructive) { confirm = "remove-keys" }
                } footer: {
                    Text("Switching off ends their sessions at once and can be undone. Removing keys signs every device out for good; they need a new invite.")
                }
                Section("Devices") {
                    ForEach(Array((u["devices"]?.array ?? []).enumerated()), id: \.offset) { _, d in
                        HStack {
                            Text(d["name"]?.string ?? "")
                            Spacer()
                            Text(d["revoked"]?.bool == true ? "Removed" : (d["last_used_at"]?.string.flatMap(ServerDate.parse).map { Fmt.dayTime($0) } ?? "Not used"))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                Section("Activity") {
                    if activity.isEmpty { Text("Nothing yet.").foregroundStyle(.secondary) }
                    ForEach(Array(activity.prefix(100).enumerated()), id: \.offset) { _, a in
                        HStack {
                            Text(a["what"]?.string ?? "")
                            Spacer()
                            Text(a["at"]?.string.flatMap(ServerDate.parse).map { Fmt.dayTime($0) } ?? "").font(.caption).foregroundStyle(.secondary)
                        }
                        .font(.subheadline)
                    }
                }
            }
        }
        .navigationTitle(user?["name"]?.string ?? "User")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .confirmationDialog(confirm == "disable" ? "Switch \(user?["name"]?.string ?? "") off?" : "Remove all Face ID keys of \(user?["name"]?.string ?? "")?",
                            isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }), titleVisibility: .visible) {
            Button(confirm == "disable" ? "Switch off" : "Remove keys", role: .destructive) {
                if let c = confirm { Task { await post(c) } }
            }
        }
    }

    private func areaRow(_ a: JSONValue) -> some View {
        let key = a["key"]?.string ?? ""
        let canUse = a["use"] != nil && a["use"] != .null
        return Picker(selection: Binding(get: { access[key] ?? "none" }, set: { access[key] = $0; Task { await save() } })) {
            Text("None").tag("none")
            Text("View").tag("view")
            if canUse { Text("Use").tag("use") }
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(a["label"]?.string ?? key)
                Text((a["view"]?.string ?? "") + (canUse ? " · Use: \(a["use"]?.string ?? "")" : "")).font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func load() async {
        do {
            all = try await model.api.raw("GET", "/v1/users")
            if let u = user {
                access = (u["access"]?.object ?? [:]).compactMapValues(\.string)
                hide = u["hide_amounts"]?.bool == true
            }
            activity = (try? await model.api.raw("GET", "/v1/users/\(userID)/activity"))?["rows"]?.array ?? []
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func save() async {
        do {
            _ = try await model.api.raw("PATCH", "/v1/users/\(userID)", body: JSONValue.object([
                "hide_amounts": .bool(hide), "access": .object(access.mapValues { JSONValue.string($0) })]))
            note = "Saved. It applies to their next request."
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func post(_ what: String) async {
        do {
            let r = try await model.api.raw("POST", "/v1/users/\(userID)/\(what)")
            confirm = nil
            note = (what == "remove-keys" ? "Face ID keys removed." : what == "disable" ? "Login switched off." : "Login switched on.")
                + (r["stop_all_sent"]?.bool == true ? " Stop all was sent for their live and shadow sessions." : "")
            await load()
        } catch { self.error = error.localizedDescription }
    }
}

/// More › Share a clean copy of the code (owner): exactly what is committed on the server, so no keys, tokens or
/// data can be in it. Opens the share sheet (AirDrop to the Mac, Files, Mail).
struct ShareCodeView: View {
    @Environment(AppModel.self) private var model
    @State private var file: URL?
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        Form {
            Section {
                Text("A zip of the code running on your server, made with git from what is committed: no secrets, keys, tokens, databases or market data. A friend sets up their own server, Zerodha keys and logins from docs/deploy.md.")
                    .font(.subheadline)
            }
            Section {
                if let file {
                    ShareLink(item: file) { Label("Share \(file.lastPathComponent)", systemImage: "square.and.arrow.up").frame(minHeight: 44) }
                } else {
                    Button(busy ? "Preparing…" : "Prepare the zip", systemImage: "shippingbox") { Task { await fetch() } }
                        .disabled(busy).frame(minHeight: 44)
                }
                if let error { Text(error).foregroundStyle(Color.tdBad) }
            }
        }
        .navigationTitle("Share the code")
    }

    private func fetch() async {
        busy = true
        defer { busy = false }
        do {
            let bytes = try await model.api.download("/v1/system/share")
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("tradedesk-code.zip")
            try bytes.write(to: url)
            file = url
        } catch { self.error = error.localizedDescription }
    }
}
