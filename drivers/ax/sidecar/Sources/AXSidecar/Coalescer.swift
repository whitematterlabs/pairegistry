import Foundation

/// Per-event-kind rate cap. AXValueChanged on a typing field alone can hit
/// 50/sec; AX_PLAN risk note demands all rate limiting live in Swift (never
/// Python). When a kind exceeds its cap inside a rolling window, subsequent
/// events of that kind are dropped and counted; a single `ax:event_rate_capped`
/// event surfaces the overflow at window close.
final class Coalescer {
    static let shared = Coalescer(perSecondCap: 20)

    private let queue = DispatchQueue(label: "ax.coalescer")
    private let cap: Int
    private let windowSec: Double = 1.0

    private struct Bucket {
        var windowStart: Date
        var emittedInWindow: Int
        var droppedInWindow: Int
    }
    private var buckets: [String: Bucket] = [:]

    init(perSecondCap: Int) {
        self.cap = perSecondCap
    }

    /// Returns true if the caller should emit; false if dropped by rate cap.
    /// On rolling over the window, if any drops happened, emits a synthetic
    /// `ax:event_rate_capped` event for the prior window.
    func shouldEmit(kind: String) -> Bool {
        queue.sync {
            let now = Date()
            var b = buckets[kind] ?? Bucket(windowStart: now,
                                            emittedInWindow: 0,
                                            droppedInWindow: 0)
            if now.timeIntervalSince(b.windowStart) >= windowSec {
                if b.droppedInWindow > 0 {
                    let dropped = b.droppedInWindow
                    let window = windowSec
                    // Async to avoid re-entering the coalescer from the emitter
                    // path while holding the queue.
                    DispatchQueue.global(qos: .utility).async {
                        NDJSONEmitter.shared.emit(
                            kind: "ax:event_rate_capped",
                            payload: [
                                "event_kind": kind,
                                "dropped": dropped,
                                "window_sec": window,
                            ]
                        )
                    }
                }
                b = Bucket(windowStart: now,
                           emittedInWindow: 0,
                           droppedInWindow: 0)
            }
            if b.emittedInWindow < cap {
                b.emittedInWindow += 1
                buckets[kind] = b
                return true
            }
            b.droppedInWindow += 1
            buckets[kind] = b
            return false
        }
    }
}
