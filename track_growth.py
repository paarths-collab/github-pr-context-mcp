"""
track_growth.py — Daily growth tracker for github-pr-context-mcp

Fetches and stores:
  - GitHub repository clone traffic (unique cloners per day, past 14 days)
  - PyPI download counts (uvx / pipx installs come through here)
  - GitHub release asset downloads (if any)

Usage:
  python track_growth.py               # fetch + print today's summary
  python track_growth.py --history 30  # show last 30 days from DB
  python track_growth.py --json        # output raw JSON

Requires:
  - GITHUB_TOKEN in environment or .env (needs repo traffic read access)
  - requests (already a project dependency)

Schedule daily with:
  Windows Task Scheduler  →  Action: python C:\path\to\track_growth.py
  Linux/Mac cron          →  0 9 * * * cd /path/to/repo && python track_growth.py
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

REPO_OWNER = "paarths-collab"
REPO_NAME = "github-pr-context-mcp"
PYPI_PACKAGE = "github-pr-context-mcp"

# DB lives alongside the rest of the analytics data
DB_PATH = Path.home() / ".github-pr-mcp" / "growth_stats.db"
GITHUB_API = "https://api.github.com"
PYPI_API = "https://pypistats.org/api"

# ── DB setup ──────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS github_clones (
            date        TEXT PRIMARY KEY,  -- YYYY-MM-DD
            total       INTEGER DEFAULT 0, -- all clone events (including repeat cloners)
            uniques     INTEGER DEFAULT 0  -- unique cloners (IP-based, GitHub's count)
        );

        CREATE TABLE IF NOT EXISTS pypi_downloads (
            date        TEXT PRIMARY KEY,  -- YYYY-MM-DD
            total       INTEGER DEFAULT 0, -- total downloads (pip install / uvx / pipx)
            with_mirrors INTEGER DEFAULT 0 -- includes CDN mirror traffic
        );

        CREATE TABLE IF NOT EXISTS github_release_downloads (
            date        TEXT PRIMARY KEY,  -- YYYY-MM-DD (snapshot date)
            total       INTEGER DEFAULT 0  -- cumulative release asset downloads
        );

        CREATE TABLE IF NOT EXISTS fetch_log (
            fetched_at  TEXT,
            source      TEXT,
            status      TEXT,
            detail      TEXT
        );
    """)


# ── GitHub traffic ─────────────────────────────────────────────────────────────

def fetch_github_clones(token: str, conn: sqlite3.Connection) -> list[dict]:
    """
    Fetch the last 14 days of clone traffic from GitHub's traffic API.
    Requires the token to have push/admin access on the repo (or be the owner).
    """
    url = f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/traffic/clones"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    resp = requests.get(url, headers=headers, timeout=15)
    now = datetime.now(timezone.utc).isoformat()

    if resp.status_code != 200:
        msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
        conn.execute("INSERT INTO fetch_log VALUES (?, 'github_clones', 'error', ?)", (now, msg))
        print(f"  [github clones] ERROR — {msg}", file=sys.stderr)
        return []

    data = resp.json().get("clones", [])
    rows = []
    for entry in data:
        # timestamp format: "2024-01-15T00:00:00Z"
        date_str = entry["timestamp"][:10]  # keep YYYY-MM-DD only
        total = entry.get("count", 0)
        uniques = entry.get("uniques", 0)
        conn.execute(
            "INSERT INTO github_clones (date, total, uniques) VALUES (?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET total = MAX(total, ?), uniques = MAX(uniques, ?)",
            (date_str, total, uniques, total, uniques),
        )
        rows.append({"date": date_str, "total": total, "uniques": uniques})

    conn.execute("INSERT INTO fetch_log VALUES (?, 'github_clones', 'ok', ?)", (now, f"{len(rows)} days fetched"))
    return rows


def fetch_github_release_downloads(token: str, conn: sqlite3.Connection) -> int:
    """
    Fetch total download count across all GitHub release assets.
    Useful if you ever attach .whl / .tar.gz files to releases.
    """
    url = f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/releases"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    resp = requests.get(url, headers=headers, timeout=15)
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()

    if resp.status_code != 200:
        msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
        conn.execute("INSERT INTO fetch_log VALUES (?, 'github_releases', 'error', ?)", (now, msg))
        return 0

    total = sum(
        asset.get("download_count", 0)
        for release in resp.json()
        for asset in release.get("assets", [])
    )

    conn.execute(
        "INSERT INTO github_release_downloads (date, total) VALUES (?, ?) "
        "ON CONFLICT(date) DO UPDATE SET total = ?",
        (today, total, total),
    )
    conn.execute("INSERT INTO fetch_log VALUES (?, 'github_releases', 'ok', ?)", (now, f"total={total}"))
    return total


# ── PyPI stats ────────────────────────────────────────────────────────────────

def fetch_pypi_downloads(conn: sqlite3.Connection) -> dict:
    """
    Fetch PyPI download stats from pypistats.org (no auth required).
    Returns recent + overall (with/without mirrors) counts.

    uvx and pipx both install from PyPI, so these numbers capture them.
    """
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()

    # Recent downloads (last day / week / month)
    recent_url = f"{PYPI_API}/packages/{PYPI_PACKAGE}/recent"
    overall_url = f"{PYPI_API}/packages/{PYPI_PACKAGE}/overall"

    result: dict = {}

    try:
        r = requests.get(recent_url, timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", {})
            result["last_day"] = data.get("last_day", 0)
            result["last_week"] = data.get("last_week", 0)
            result["last_month"] = data.get("last_month", 0)
        else:
            print(f"  [pypi recent] HTTP {r.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  [pypi recent] {e}", file=sys.stderr)

    try:
        r = requests.get(overall_url, timeout=15)
        if r.status_code == 200:
            rows = r.json().get("data", [])
            # rows is a list of {category, date, downloads}
            # category = "with_mirrors" or "without_mirrors"
            for row in rows:
                if row.get("date") == today:
                    cat = row.get("category", "")
                    dl = row.get("downloads", 0)
                    if cat == "with_mirrors":
                        result["today_with_mirrors"] = dl
                    elif cat == "without_mirrors":
                        result["today_without_mirrors"] = dl
    except Exception as e:
        print(f"  [pypi overall] {e}", file=sys.stderr)

    # Store today's snapshot
    total = result.get("last_day", 0)
    with_mirrors = result.get("today_with_mirrors", 0)
    conn.execute(
        "INSERT INTO pypi_downloads (date, total, with_mirrors) VALUES (?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET total = MAX(total, ?), with_mirrors = MAX(with_mirrors, ?)",
        (today, total, with_mirrors, total, with_mirrors),
    )
    conn.execute("INSERT INTO fetch_log VALUES (?, 'pypi', 'ok', ?)", (now, json.dumps(result)))
    return result


# ── Display ───────────────────────────────────────────────────────────────────

def print_summary(conn: sqlite3.Connection, days: int = 14) -> None:
    print(f"\n{'='*62}")
    print(f"  github-pr-context-mcp  |  Growth Dashboard")
    print(f"{'='*62}")

    # GitHub clones
    print(f"\n  [GitHub] Clones (last {days} days)\n")
    print(f"  {'Date':<12} {'Unique Cloners':>14}  {'Total Clone Events':>18}")
    print(f"  {'-'*12} {'-'*14}  {'-'*18}")
    clones = conn.execute(
        "SELECT date, uniques, total FROM github_clones ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    if clones:
        for row in clones:
            print(f"  {row['date']:<12} {row['uniques']:>14,}  {row['total']:>18,}")
        total_uniques = sum(r["uniques"] for r in clones)
        print(f"  {'-'*12} {'-'*14}  {'-'*18}")
        print(f"  {'TOTAL':<12} {total_uniques:>14,}")
    else:
        print("  No data yet.")

    # PyPI downloads
    print(f"\n  [PyPI] Downloads (uvx / pipx / pip)\n")
    pypi = conn.execute(
        "SELECT date, total, with_mirrors FROM pypi_downloads ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    if pypi:
        print(f"  {'Date':<12} {'Downloads':>10}  {'With Mirrors':>12}")
        print(f"  {'-'*12} {'-'*10}  {'-'*12}")
        for row in pypi:
            print(f"  {row['date']:<12} {row['total']:>10,}  {row['with_mirrors']:>12,}")
    else:
        print("  No data yet (package may not be on PyPI, or no downloads today).")

    # GitHub release downloads
    print(f"\n  [GitHub] Release Asset Downloads\n")
    releases = conn.execute(
        "SELECT date, total FROM github_release_downloads ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if releases and releases["total"] > 0:
        print(f"  Latest snapshot ({releases['date']}): {releases['total']:,} total downloads")
    else:
        print("  No release assets found (installs via PyPI are tracked above).")

    print(f"\n{'='*62}\n")


# ── Export ────────────────────────────────────────────────────────────────────

EXPORT_PATH = Path(__file__).parent / "metrics" / "clone-traffic.json"


def export_clone_traffic(conn: sqlite3.Connection, path: Path = EXPORT_PATH) -> dict:
    """Write the full recorded clone history to a JSON file.

    GitHub's traffic API only serves a rolling 14-day window, so anything older
    exists nowhere else once it ages out of the local database. Committing this
    file is what turns a disappearing window into a permanent record — which
    also means a day never fetched is a day lost for good, so the export names
    its gaps rather than presenting the series as continuous.
    """
    rows = conn.execute(
        "SELECT date, total, uniques FROM github_clones ORDER BY date ASC"
    ).fetchall()

    daily = [
        {"date": r["date"], "clones": r["total"], "unique_cloners": r["uniques"]}
        for r in rows
    ]

    gaps: list[str] = []
    if daily:
        first = datetime.strptime(daily[0]["date"], "%Y-%m-%d").date()
        last = datetime.strptime(daily[-1]["date"], "%Y-%m-%d").date()
        recorded = {d["date"] for d in daily}
        for offset in range((last - first).days + 1):
            day = (first + timedelta(days=offset)).isoformat()
            if day not in recorded:
                gaps.append(day)

    recent = daily[-14:]
    payload = {
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "GitHub repository traffic API (/traffic/clones)",
        "window": {
            "first_day": daily[0]["date"] if daily else None,
            "last_day": daily[-1]["date"] if daily else None,
            "days_recorded": len(daily),
            "missing_days": gaps,
            "note": (
                "GitHub retains only 14 days. Days absent from missing_days but "
                "outside the current window were captured by an earlier run."
            ),
        },
        "totals": {
            "clones": sum(d["clones"] for d in daily),
            "sum_of_daily_unique_cloners": sum(d["unique_cloners"] for d in daily),
            "note": (
                "unique_cloners is GitHub's per-day IP-based count. Summing it "
                "across days double-counts anyone who cloned on more than one "
                "day, so the total is an upper bound, not a distinct-person count."
            ),
        },
        "last_14_recorded_days": {
            "first_day": recent[0]["date"] if recent else None,
            "last_day": recent[-1]["date"] if recent else None,
            "clones": sum(d["clones"] for d in recent),
            "sum_of_daily_unique_cloners": sum(d["unique_cloners"] for d in recent),
        },
        "daily": daily,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Track daily growth stats for github-pr-context-mcp")
    parser.add_argument("--history", type=int, default=14, metavar="DAYS",
                        help="Number of days to show in summary (default: 14)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of table")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip fetching new data, just show DB contents")
    parser.add_argument("--export", nargs="?", const=str(EXPORT_PATH), metavar="PATH",
                        help=f"Write full clone history to JSON (default: {EXPORT_PATH})")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")

    conn = _get_conn()
    _init_db(conn)

    if not args.no_fetch:
        print("Fetching data...")

        if not token:
            print("  [!] GITHUB_TOKEN not set -- skipping GitHub traffic fetch.")
            print("      Add GITHUB_TOKEN=ghp_xxx to your .env file or environment.")
            print("      The token needs read access to repository traffic.\n")
        else:
            print("  Fetching GitHub clone traffic...", end=" ", flush=True)
            clones = fetch_github_clones(token, conn)
            print(f"got {len(clones)} days")

            print("  Fetching GitHub release downloads...", end=" ", flush=True)
            release_dl = fetch_github_release_downloads(token, conn)
            print(f"total = {release_dl:,}")

        print("  Fetching PyPI download stats...", end=" ", flush=True)
        pypi = fetch_pypi_downloads(conn)
        print(f"last_day={pypi.get('last_day', 'N/A')}, last_week={pypi.get('last_week', 'N/A')}, last_month={pypi.get('last_month', 'N/A')}")

    if args.json:
        clones = [dict(r) for r in conn.execute(
            "SELECT date, uniques, total FROM github_clones ORDER BY date DESC LIMIT ?", (args.history,)
        ).fetchall()]
        pypi_rows = [dict(r) for r in conn.execute(
            "SELECT date, total, with_mirrors FROM pypi_downloads ORDER BY date DESC LIMIT ?", (args.history,)
        ).fetchall()]
        print(json.dumps({"github_clones": clones, "pypi_downloads": pypi_rows}, indent=2))
    else:
        print_summary(conn, days=args.history)

    if args.export:
        payload = export_clone_traffic(conn, Path(args.export))
        window, totals = payload["window"], payload["totals"]
        print(f"  Exported {window['days_recorded']} days to {args.export}")
        print(f"    {window['first_day']} -> {window['last_day']}, "
              f"{totals['clones']:,} clones")
        if window["missing_days"]:
            print(f"    [!] {len(window['missing_days'])} day(s) missing inside that "
                  f"range and unrecoverable: {', '.join(window['missing_days'][:5])}"
                  f"{' ...' if len(window['missing_days']) > 5 else ''}")


if __name__ == "__main__":
    main()
