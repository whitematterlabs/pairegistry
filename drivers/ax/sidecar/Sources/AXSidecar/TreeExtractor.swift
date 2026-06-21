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
    struct Snapshot {
        let tree: [[String: Any]]
        let diagnostics: [String: Any]
    }

    private struct Stats {
        var nodeCount = 0
        var actionableCount = 0
        var usefulActionableCount = 0
        var standardWindowButtonCount = 0
        var structuralCount = 0
        var maxDepth = 0
        var roleCounts: [String: Int] = [:]
    }

    /// Render tree + diagnostics together. Diagnostics are intentionally
    /// cheap and role/count based: they tell the caller when AX sees only
    /// window chrome or opaque containers, without dumping sensitive text.
    static func snapshot(session: Session) -> Snapshot {
        let tree = dump(session: session)
        let diagnostics = diagnose(session: session)
        return Snapshot(tree: tree, diagnostics: diagnostics)
    }

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
        let role = AXHelpers.role(element)
        let children = AXHelpers.childrenAttr(element)

        let isInteractive = AXHelpers.interactiveRoles.contains(role)
        let isStructural = AXHelpers.structuralRoles.contains(role)
        let label = label(for: element)
        let hasUsefulDescendant = hasUsefulActionableDescendant(element)
        let keepStructural = isStructural
            && !isInteractive
            && (!label.isEmpty || (!hasUsefulDescendant && !children.isEmpty))
        let isOpaqueStructural = keepStructural && !hasUsefulDescendant

        if isInteractive || keepStructural {
            let ref = session.allocRef(element)
            var entry: [String: Any] = [
                "ref": ref,
                "role": role,
                "label": label,
                "value": AXHelpers.elementText(element),
                "enabled": AXHelpers.boolAttr(element, .enabled) ?? true,
            ]
            let subrole = AXHelpers.subrole(element)
            if !subrole.isEmpty {
                entry["subrole"] = subrole
            }
            if !children.isEmpty {
                entry["children"] = children.count
            }
            if !isInteractive {
                entry["actionable"] = false
                entry["opaque"] = isOpaqueStructural
            } else if AXHelpers.isStandardWindowButton(element) {
                entry["window_chrome"] = true
            }
            out.append(entry)
            // Interactive nodes may also have meaningful children (e.g. a
            // popup menu's items). We still recurse but stop adding labels.
        }

        if AXHelpers.isStandardWindowButton(element) {
            return
        }
        if isOpaqueStructural {
            return
        }

        for child in children {
            walk(child, session: session, into: &out, depth: depth + 1, maxDepth: maxDepth)
        }
    }

    /// Best-effort human label: AXTitle, AXDescription, AXHelp, AXValue
    /// (in that order). Truncated.
    private static func label(for element: UIElement) -> String {
        if let t = AXHelpers.rawStringAttr(element, kAXTitleAttribute), !t.isEmpty { return AXHelpers.truncated(t, max: 80) }
        if let d = AXHelpers.rawStringAttr(element, kAXDescriptionAttribute), !d.isEmpty { return AXHelpers.truncated(d, max: 80) }
        if let h = AXHelpers.rawStringAttr(element, kAXHelpAttribute), !h.isEmpty { return AXHelpers.truncated(h, max: 80) }
        if let v = AXHelpers.rawStringAttr(element, kAXValueAttribute), !v.isEmpty { return AXHelpers.truncated(v, max: 80) }
        let subrole = AXHelpers.subrole(element)
        switch subrole {
        case "AXCloseButton": return "close button"
        case "AXMinimizeButton": return "minimize button"
        case "AXZoomButton": return "zoom button"
        case "AXFullScreenButton": return "full screen button"
        default: break
        }
        return ""
    }

    private static func hasUsefulActionableDescendant(_ element: UIElement,
                                                      depth: Int = 0,
                                                      maxDepth: Int = 12) -> Bool {
        if depth > maxDepth { return false }
        for child in AXHelpers.childrenAttr(element) {
            let role = AXHelpers.role(child)
            if AXHelpers.interactiveRoles.contains(role)
                && !AXHelpers.isStandardWindowButton(child) {
                return true
            }
            if hasUsefulActionableDescendant(child, depth: depth + 1, maxDepth: maxDepth) {
                return true
            }
        }
        return false
    }

    private static func diagnose(session: Session) -> [String: Any] {
        var stats = Stats()
        collectStats(session.window, into: &stats, depth: 0, maxDepth: 30)

        var warnings: [String] = []
        if stats.usefulActionableCount == 0 {
            if stats.standardWindowButtonCount > 0 {
                warnings.append(
                    "opaque_ax_tree: only standard window controls are actionable; app content is not exposed through AXChildren"
                )
            } else {
                warnings.append(
                    "opaque_ax_tree: no actionable controls exposed through AXChildren"
                )
            }
        }
        if stats.structuralCount > 0 && stats.usefulActionableCount == 0 {
            warnings.append(
                "non-actionable containers are included with actionable=false for expand/diagnostics"
            )
        }

        return [
            "node_count": stats.nodeCount,
            "actionable_count": stats.actionableCount,
            "useful_actionable_count": stats.usefulActionableCount,
            "standard_window_button_count": stats.standardWindowButtonCount,
            "structural_count": stats.structuralCount,
            "max_depth": stats.maxDepth,
            "role_counts": stats.roleCounts,
            "warnings": warnings,
        ]
    }

    private static func collectStats(_ element: UIElement,
                                     into stats: inout Stats,
                                     depth: Int,
                                     maxDepth: Int) {
        if depth > maxDepth { return }
        let role = AXHelpers.role(element)
        stats.nodeCount += 1
        stats.maxDepth = max(stats.maxDepth, depth)
        stats.roleCounts[role, default: 0] += 1

        if AXHelpers.interactiveRoles.contains(role) {
            stats.actionableCount += 1
            if AXHelpers.isStandardWindowButton(element) {
                stats.standardWindowButtonCount += 1
            } else {
                stats.usefulActionableCount += 1
            }
        }
        if AXHelpers.structuralRoles.contains(role) {
            stats.structuralCount += 1
        }
        for child in AXHelpers.childrenAttr(element) {
            collectStats(child, into: &stats, depth: depth + 1, maxDepth: maxDepth)
        }
    }
}
