import Foundation
import Carbon.HIToolbox

/// Queryable wrapper around IsSecureEventInputEnabled().
///
/// In the piloting model this is a *getter*, not an emitter. The Actuator
/// calls `isActive` before any keystroke-synthesis path; if secure input
/// is on (password field, sudo prompt, 1Password unlock), the action is
/// refused with `ESECUREINPUT`. No system-wide stream lands on the kernel
/// bus — that channel was the keylogger surface we deliberately removed.
final class SecureInputPoller {
    static let shared = SecureInputPoller()

    private init() {}

    var isActive: Bool {
        return IsSecureEventInputEnabled()
    }
}
