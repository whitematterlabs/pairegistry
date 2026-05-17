import Foundation
import Carbon.HIToolbox

/// Polls IsSecureEventInputEnabled() at 2Hz, emits transition events.
///
/// Secure Input is set whenever the user is typing into a password field,
/// 1Password unlock, sudo prompt, etc. Phase 2 ax-pilot keystroke-synth
/// paths must check this and bail. Phase 1 just records the transitions
/// so the event log has them.
final class SecureInputPoller {
    private var timer: DispatchSourceTimer?
    private var lastActive = false

    func start() {
        let t = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
        t.schedule(deadline: .now() + .milliseconds(500),
                   repeating: .milliseconds(500))
        t.setEventHandler { [weak self] in
            self?.tick()
        }
        timer = t
        t.resume()
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    private func tick() {
        let active = IsSecureEventInputEnabled()
        if active == lastActive { return }
        lastActive = active
        if active {
            NDJSONEmitter.shared.emit(kind: "ax:secure_input_active")
        } else {
            NDJSONEmitter.shared.emit(kind: "ax:secure_input_cleared")
        }
    }
}
