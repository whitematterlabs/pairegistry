import Foundation
import ApplicationServices
import AXSwift

/// Flatten an AX subtree into a compressed actionable surface.
///
/// Walking rule: depth-first, prune non-interactive containers, keep
/// interactive nodes plus immediately adjacent labels. Refs are allocated
/// against the session's ref table as we go, so subsequent `act(ref, …)`
/// and `expand(ref)` calls can resolve back to the underlying AXUIElement.
enum TreeExtractor {

    /// Render the full session-scoped tree starting at `session.window`.
    /// Returns a JSON-serializable list of `{ref, role, label, value, enabled}`.
    static func dump(session: Session) -> [[String: Any]] {
        var out: [[String: Any]] = []
        walk(session.window, session: session, into: &out, depth: 0, maxDepth: 12)
        return out
    }

    /// Children of a single ref. Used by `expand(ref)` to drill in past a
    /// container that the initial dump pruned.
    static func expand(session: Session, ref: Int) -> [[String: Any]] {
        guard let el = session.element(forRef: ref) else { return [] }
        var out: [[String: Any]] = []
        for child in AXHelpers.childrenAttr(el) {
            walk(child, session: session, into: &out, depth: 0, maxDepth: 10)
        }
        return out
    }

    // MARK: - Walk

    private static func walk(_ element: UIElement,
                             session: Session,
                             into out: inout [[String: Any]],
                             depth: Int,
                             maxDepth: Int) {
        if depth > maxDepth { return }
        let role = AXHelpers.stringAttr(element, .role) ?? ""

        let isInteractive = AXHelpers.interactiveRoles.contains(role)
        if isInteractive {
            let ref = session.allocRef(element)
            let entry: [String: Any] = [
                "ref": ref,
                "role": role,
                "label": label(for: element),
                "value": AXHelpers.elementText(element),
                "enabled": AXHelpers.boolAttr(element, .enabled) ?? true,
            ]
            out.append(entry)
            // Interactive nodes may also have meaningful children (e.g. a
            // popup menu's items). We still recurse but stop adding labels.
        }

        for child in AXHelpers.childrenAttr(element) {
            walk(child, session: session, into: &out, depth: depth + 1, maxDepth: maxDepth)
        }
    }

    /// Best-effort human label: AXTitle, AXDescription, AXHelp, AXValue
    /// (in that order). Truncated.
    private static func label(for element: UIElement) -> String {
        if let t = AXHelpers.stringAttr(element, .title), !t.isEmpty { return AXHelpers.truncated(t, max: 80) }
        if let d = AXHelpers.stringAttr(element, .description), !d.isEmpty { return AXHelpers.truncated(d, max: 80) }
        if let h = AXHelpers.stringAttr(element, .help), !h.isEmpty { return AXHelpers.truncated(h, max: 80) }
        if let v = AXHelpers.stringAttr(element, .value), !v.isEmpty { return AXHelpers.truncated(v, max: 80) }
        return ""
    }
}
