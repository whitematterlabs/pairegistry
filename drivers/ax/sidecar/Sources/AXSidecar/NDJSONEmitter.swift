import Foundation

/// Line-buffered NDJSON writer to stdout. One JSON object per line, terminated
/// by \n. All access serialized through an internal queue so concurrent emitters
/// can't interleave half-lines.
///
/// In the piloting model every event is point-to-point: the caller stamps
/// `targetPID` on emit, the supervisor (Python `inbound.py`) hands it to
/// `P.emit_event(..., target_pid=...)`, and the kernel routes only to that
/// pid. There is no broadcast path here.
///
/// Format on the wire:
///   {"kind":"ax:scope_attached","ts":..., "target_pid":2,
///    "session_id":"s1","pid":4711,"bundle_id":"com.apple.calculator", ...}
final class NDJSONEmitter {
    static let shared = NDJSONEmitter()

    private let queue = DispatchQueue(label: "ax.ndjson", qos: .userInitiated)
    private let stdout = FileHandle.standardOutput

    private init() {}

    /// Emit one event. `targetPID` is required for routable events; pass
    /// `nil` only for the boot-time `ax:permission_lost` notice (kernel
    /// surfaces that via the supervisor log, not via the bus).
    func emit(kind: String,
              targetPID: Int? = nil,
              extra: [String: Any] = [:]) {
        var obj: [String: Any] = [
            "kind": kind,
            "ts": Date().timeIntervalSince1970,
        ]
        if let t = targetPID { obj["target_pid"] = t }
        for (k, v) in extra { obj[k] = sanitize(v) }

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
