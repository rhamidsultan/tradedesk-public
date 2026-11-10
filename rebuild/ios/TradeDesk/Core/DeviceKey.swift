import CryptoKit
import Foundation
import LocalAuthentication

/// The sign-in key. On a real iPhone it lives in the Secure Enclave and every use needs Face ID
/// (or the passcode if Face ID fails). Only the public key ever leaves the phone.
///
/// The server checks: ECDSA P-256 / SHA-256 over
///   enrol:   "TD2 enroll v1|<invite code>"
///   sign-in: "TD2 login v1|<challenge id>|<nonce exactly as the server sent it>"
enum SignMessage {
    static func enroll(code: String) -> Data { Data("TD2 enroll v1|\(code)".utf8) }
    static func login(challengeID: String, nonce: String) -> Data { Data("TD2 login v1|\(challengeID)|\(nonce)".utf8) }
    /// Arming one live session for one day (the server checks the same text).
    static func arm(challengeID: String, nonce: String, sessionID: Int, day: String) -> Data {
        Data("TD2 arm v1|\(challengeID)|\(nonce)|session \(sessionID)|\(day)|LIVE".utf8)
    }
}

struct DeviceKey {
    private static let keychainKey = "device-key"

    let publicKeyX963: Data
    private let sign: (Data, LAContext?) throws -> Data

    var keyID: String {
        SHA256.hash(data: publicKeyX963).map { String(format: "%02x", $0) }.joined()
    }

    func signature(for message: Data, reason: String) throws -> Data {
        let context = LAContext()
        context.localizedReason = reason
        return try sign(message, context)
    }

    static var exists: Bool { Keychain.data(for: keychainKey) != nil }

    static func remove() { Keychain.delete(keychainKey) }

    /// Creates a new key, replacing any old one.
    static func create() throws -> DeviceKey {
        #if targetEnvironment(simulator)
        // The simulator has no Secure Enclave; a software key keeps the flow testable. Never used on a phone.
        let key = P256.Signing.PrivateKey()
        try Keychain.set(key.rawRepresentation, for: keychainKey)
        return DeviceKey(software: key)
        #else
        var error: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(
            nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, [.privateKeyUsage, .biometryCurrentSet], &error
        ) else { throw error!.takeRetainedValue() as Error }
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
        // dataRepresentation is an encrypted handle that only this phone's Secure Enclave can use
        try Keychain.set(key.dataRepresentation, for: keychainKey)
        return DeviceKey(enclave: key)
        #endif
    }

    static func load() throws -> DeviceKey? {
        guard let data = Keychain.data(for: keychainKey) else { return nil }
        #if targetEnvironment(simulator)
        return DeviceKey(software: try P256.Signing.PrivateKey(rawRepresentation: data))
        #else
        return DeviceKey(enclave: try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: data))
        #endif
    }

    #if targetEnvironment(simulator)
    private init(software key: P256.Signing.PrivateKey) {
        publicKeyX963 = key.publicKey.x963Representation
        sign = { message, _ in try key.signature(for: message).derRepresentation }
    }
    #else
    private init(enclave key: SecureEnclave.P256.Signing.PrivateKey) {
        publicKeyX963 = key.publicKey.x963Representation
        let handle = key.dataRepresentation
        sign = { message, context in
            // re-open the key with this context so Face ID shows our reason text
            let k = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: handle, authenticationContext: context)
            return try k.signature(for: message).derRepresentation
        }
    }
    #endif

    /// For tests: a software key with the same behaviour.
    init(testKey key: P256.Signing.PrivateKey) {
        publicKeyX963 = key.publicKey.x963Representation
        sign = { message, _ in try key.signature(for: message).derRepresentation }
    }
}
