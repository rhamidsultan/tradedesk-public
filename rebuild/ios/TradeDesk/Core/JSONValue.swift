import Foundation

/// A strategy spec edited as plain JSON, so every setting the server knows survives a round trip, even ones this
/// version of the app has no screen for. Paths are dotted ("risk.stop_loss.value", "legs.0.side").
enum JSONValue: Codable, Equatable, Hashable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): if n == n.rounded(), abs(n) < 1e15 { try c.encode(Int(n)) } else { try c.encode(n) }
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }

    var string: String? { if case .string(let s) = self { return s }; return nil }
    var number: Double? { if case .number(let n) = self { return n }; return nil }
    var bool: Bool? { if case .bool(let b) = self { return b }; return nil }
    var array: [JSONValue]? { if case .array(let a) = self { return a }; return nil }
    var object: [String: JSONValue]? { if case .object(let o) = self { return o }; return nil }

    subscript(path: String) -> JSONValue? {
        get {
            var cur: JSONValue? = self
            for p in path.split(separator: ".").map(String.init) {
                if let i = Int(p), case .array(let a)? = cur { cur = a.indices.contains(i) ? a[i] : nil }
                else if case .object(let o)? = cur { cur = o[p] }
                else { return nil }
            }
            return cur
        }
        set { self = Self.setting(self, Array(path.split(separator: ".").map(String.init)), newValue) }
    }

    private static func setting(_ node: JSONValue, _ keys: [String], _ value: JSONValue?) -> JSONValue {
        guard let k = keys.first else { return value ?? .null }
        let rest = Array(keys.dropFirst())
        if let i = Int(k), case .array(var a) = node {
            while a.count <= i { a.append(.null) }
            a[i] = setting(a[i], rest, value)
            return .array(a)
        }
        var o = node.object ?? [:]
        if rest.isEmpty, value == nil { o.removeValue(forKey: k) } else { o[k] = setting(o[k] ?? (Int(rest.first ?? "") != nil ? .array([]) : .object([:])), rest, value) }
        return .object(o)
    }

    static let rawDecoder = JSONDecoder()
}
