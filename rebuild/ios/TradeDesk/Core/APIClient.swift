import Foundation

enum APIError: Error, LocalizedError, Equatable {
    case http(Int, String, Data = Data())
    case unauthorized(String)
    case transport(String)
    case notConfigured

    var errorDescription: String? {
        switch self {
        case .http(_, let m, _), .unauthorized(let m), .transport(let m): return m
        case .notConfigured: return "Add the server address first."
        }
    }
}

/// Talks to the TradeDesk server over HTTPS with the session token from sign-in.
final class APIClient: @unchecked Sendable {
    var baseURL: URL?
    var token: String?
    private let session: URLSession

    init(baseURL: URL? = nil, token: String? = nil, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        d.dateDecodingStrategy = .custom { dec in
            let s = try dec.singleValueContainer().decode(String.self)
            guard let date = ServerDate.parse(s) else {
                throw DecodingError.dataCorrupted(.init(codingPath: dec.codingPath, debugDescription: "bad date \(s)"))
            }
            return date
        }
        return d
    }()

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.dateEncodingStrategy = .iso8601
        return e
    }()

    func get<T: Decodable>(_ path: String) async throws -> T {
        try await send("GET", path, body: Optional<Empty>.none)
    }

    func post<T: Decodable, B: Encodable>(_ path: String, _ body: B) async throws -> T {
        try await send("POST", path, body: body)
    }

    func post<T: Decodable>(_ path: String) async throws -> T {
        try await send("POST", path, body: Optional<Empty>.none)
    }

    func put<T: Decodable, B: Encodable>(_ path: String, _ body: B) async throws -> T {
        try await send("PUT", path, body: body)
    }

    func patch<T: Decodable, B: Encodable>(_ path: String, _ body: B) async throws -> T {
        try await send("PATCH", path, body: body)
    }

    private struct Empty: Codable {}
    private struct ErrorBody: Decodable { let detail: DetailValue? }
    private enum DetailValue: Decodable {
        case text(String), other
        init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if let s = try? c.decode(String.self) { self = .text(s) }
            else if let o = try? c.decode([String: JSONValue].self), let m = o["message"]?.string { self = .text(m) }
            else { self = .other }
        }
    }

    /// For strategy specs: no key conversion, so "no_new_after" stays as the server wrote it.
    func raw(_ method: String, _ path: String, body: (any Encodable)? = nil) async throws -> JSONValue {
        let encoded = try body.map { try JSONEncoder().encode(AnyEncodable($0)) }   // keys exactly as given
        let data = try await sendData(method, path, encoded: encoded)
        return try JSONValue.rawDecoder.decode(JSONValue.self, from: data)
    }

    /// A file the server sends (CSV, zip): the raw bytes.
    func download(_ path: String) async throws -> Data {
        try await sendData("GET", path, encoded: nil)
    }

    func send<T: Decodable, B: Encodable>(_ method: String, _ path: String, body: B?) async throws -> T {
        let encoded = try body.map { try Self.encoder.encode($0) }
        return try Self.decoder.decode(T.self, from: try await sendData(method, path, encoded: encoded))
    }

    private func sendData(_ method: String, _ path: String, encoded: Data?) async throws -> Data {
        guard let base = baseURL, let url = URL(string: path, relativeTo: base) else { throw APIError.notConfigured }
        var req = URLRequest(url: url, timeoutInterval: 20)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let encoded {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = encoded
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw APIError.transport("Can't reach the server. Check the connection.")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            var message = "Server error (\(status))."
            if let e = try? Self.decoder.decode(ErrorBody.self, from: data), case .text(let m)? = e.detail { message = m }
            if status == 401 { throw APIError.unauthorized(message) }
            throw APIError.http(status, message, data)
        }
        return data
    }
}

struct AnyEncodable: Encodable {
    let value: any Encodable
    init(_ v: any Encodable) { value = v }
    func encode(to encoder: Encoder) throws { try value.encode(to: encoder) }
}

/// Python's isoformat(): "2026-11-03T04:30:00.123456+00:00" or without the fraction.
enum ServerDate {
    static func parse(_ s: String) -> Date? {
        var text = s
        // keep at most 3 fractional digits; ISO8601DateFormatter handles milliseconds reliably
        if let dot = text.firstIndex(of: "."),
           let end = text[dot...].firstIndex(where: { $0 == "+" || $0 == "-" || $0 == "Z" }) {
            let digits = text[text.index(after: dot)..<end]
            text.replaceSubrange(text.index(after: dot)..<end, with: String(digits.prefix(3)).padding(toLength: 3, withPad: "0", startingAt: 0))
        }
        let f = ISO8601DateFormatter()
        f.formatOptions = text.contains(".") ? [.withInternetDateTime, .withFractionalSeconds] : [.withInternetDateTime]
        return f.date(from: text)
    }
}
