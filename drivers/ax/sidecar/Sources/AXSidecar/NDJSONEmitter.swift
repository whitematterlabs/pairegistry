import Foundation

/// Line-buffered NDJSON writer to stdout. One JSON object per line, terminated
/// by \n. All access serialized through an internal queue so concurrent emitters
/// can't interleave half-lines.
///
/// Format on the wire:
///   {"kind":"ax:focus_changed","ts":1715900000.123,"pid":4711,
///    "bundle_id":"com.apple.mail","payload":{...}}
final class NDJSONEmitter {
    static let shared = NDJSONEmitter()

    private let queue = DispatchQueue(label: "ax.ndjson", qos: .userInitiated)
    private let stdout = FileHandle.standardOutput

    private init() {}

    /// Emit one event. `payload` is whatever fields fit the event kind;
    /// the wrapper adds kind/ts and optional pid/bundle_id.
    func emit(kind: String,
              pid: Int32? = nil,
              bundleID: String? = nil,
              payload: [String: Any] = [:]) {
        var obj: [String: Any] = [
            "kind": kind,
            "ts": Date().timeIntervalSince1970,
        ]
        if let pid = pid { obj["pid"] = Int(pid) }
        if let bid = bundleID { obj["bundle_id"] = bid }
        if !payload.isEmpty { obj["payload"] = sanitize(payload) }

        queue.async { [weak self] in
            guard let self = self else { return }
            do {
                let data = try JSONSerialization.data(
                    withJSONObject: obj,
                    options: [.fragmentsAllowed]
                )
                self.stdout.write(data)
                self.stdout.write(Data([0x0a]))  // \n
            } catch {
                FileHandle.standardError.write(
                    Data("axd: json encode failed for \(kind): \(error)\n".utf8))
            }
        }
    }

    /// JSONSerialization rejects values it doesn't recognize (NSNull-as-String,
    /// CGFloat, etc). Coerce common AX types into JSON-safe primitives.
    private func sanitize(_ value: Any) -> Any {
        if let dict = value as? [String: Any] {
            var out: [String: Any] = [:]
            for (k, v) in dict { out[k] = sanitize(v) }
            return out
        }
        if let arr = value as? [Any] {
            return arr.map { sanitize($0) }
        }
        if let n = value as? NSNumber { return n }
        if let s = value as? String { return s }
        if let b = value as? Bool { return b }
        if let i = value as? Int { return i }
        if let d = value as? Double { return d }
        if let f = value as? CGFloat { return Double(f) }
        if let url = value as? URL { return url.absoluteString }
        return String(describing: value)
    }
}
