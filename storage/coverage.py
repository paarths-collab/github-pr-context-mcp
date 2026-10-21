# Plain-language description of what an index actually covers.
# Pure functions over the bounds dict from CursorStore.get_bounds — no I/O here.


def history_complete(bounds: dict | None) -> bool | None:
    """True/False when known; None when the import extent was never recorded."""
    if not bounds or bounds.get("history_truncated") is None:
        return None
    return not bounds["history_truncated"]


def describe_coverage(bounds: dict | None) -> str:
    """One sentence an agent can quote so it never overstates what was imported."""
    scope = "merged and closed pull requests only (open PRs are never fetched)"
    if not bounds:
        return (
            "Import bounds were not recorded for this index (built before bound "
            "tracking). Treat its history coverage as unknown; delete and re-index "
            f"to record it. Scope: {scope}."
        )
    first, last = bounds["oldest_pr_number"], bounds["newest_pr_number"]
    dates = (
        f"{(bounds['oldest_pr_updated_at'] or '?')[:10]} to "
        f"{(bounds['newest_pr_updated_at'] or '?')[:10]}"
    )
    summary = (
        f"{bounds['indexed_pr_count']} PRs indexed (#{first} to #{last}, "
        f"last updated {dates})"
    )
    limit = bounds.get("configured_pr_limit")
    cap = f", import limit {limit} PRs ({bounds['configured_pages']} pages)" if limit else ""
    truncated = bounds.get("history_truncated")
    if truncated is None:
        return (
            f"{summary}{cap}. How far the original import reached was not recorded, "
            f"so older history may be missing. Scope: {scope}."
        )
    if truncated:
        return (
            f"PARTIAL HISTORY: {summary}{cap}. GitHub has older PRs that were NOT "
            f"imported. Scope: {scope}."
        )
    return (
        f"{summary}{cap}. GitHub reported no older PRs beyond the oldest imported. "
        f"Scope: {scope}."
    )
