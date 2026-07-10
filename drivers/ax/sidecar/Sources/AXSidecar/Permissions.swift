import Foundation
import Darwin

/// Capability gate for GUI actuation. The `ax` sidecar can drive any app's
/// controls, which is a full bypass of the kernel's per-channel send freezes
/// (it reaches an app's own Send button instead of the frozen outbound
/// driver). So every `act` is checked here first.
///
/// Enforcement lives in the sidecar — a process the PAI does not control —
/// on purpose: a gate in the `ax` Python client would be worthless because the
/// PAI has a shell and can speak JSON-RPC to the socket directly. This is the
/// trust boundary, exactly as the outbound driver process is for sends.
///
/// The signal is the same freeze-file convention the send drivers use: a file
/// under `$PAI_ROOT/sys/drivers/<driver>/<name>.freeze` means "frozen". The
/// kernel's `project_capabilities()` writes/removes these in lockstep with
/// `capabilities:` in etc/config.yaml on boot and every reload, so flipping a
/// capability takes effect on the very next actuation with no restart. Files
/// are read fresh each call — never cached — so a live downgrade is honored
/// immediately.
enum Permissions {

    /// PAI_ROOT, resolved the same way main.swift does (env, else real home).
    static let paiRoot: String = {
        if let r = ProcessInfo.processInfo.environment["PAI_ROOT"], !r.isEmpty {
            return r
        }
        if let pw = getpwuid(getuid()), let home = pw.pointee.pw_dir {
            return String(cString: home) + "/.pai"
        }
        return NSHomeDirectory() + "/.pai"
    }()

    /// Bundle id → the send driver whose `outbound.freeze` also gates driving
    /// that app's GUI. Pressing Send in Messages must be gated by the same
    /// `imessage_send` capability that gates the outbound driver, or `ax`
    /// becomes an ungated side door to the exact send the owner froze.
    static let sendDriverForBundle: [String: String] = [
        "com.apple.MobileSMS": "imessage",     // Messages
        "com.apple.mail": "email",             // Mail
        "com.tinyspeck.slackmacgap": "slack",  // Slack
        "net.whatsapp.WhatsApp": "whatsapp",   // WhatsApp (Mac App Store)
        "WhatsApp": "whatsapp",                // WhatsApp (older/Electron)
        "desktop.WhatsApp": "whatsapp",        // WhatsApp (alt bundle)
    ]

    private static func frozen(_ driver: String, _ file: String) -> Bool {
        FileManager.default.fileExists(
            atPath: paiRoot + "/sys/drivers/\(driver)/\(file)")
    }

    /// A capability name to refuse with, or nil if this actuation is allowed.
    ///
    /// Two independent gates, both fail-closed (freeze present = denied):
    ///   1. Blunt: `computer_use` must be `yes` (ax/control.freeze absent) for
    ///      ANY actuation. Default-off, so an unconfigured system is locked.
    ///   2. Per-app: the attached app's send capability must be `yes` for a
    ///      messaging app, so a granted computer_use still can't press Send in
    ///      a frozen channel.
    static func denyReason(bundleID: String) -> String? {
        if frozen("ax", "control.freeze") {
            return "computer_use"
        }
        if let driver = sendDriverForBundle[bundleID],
           frozen(driver, "outbound.freeze") {
            return "\(driver)_send"
        }
        return nil
    }
}
