import argparse
from fetcher import fetch_prs
from storage import index_prs, get_collection_stats

def main():
    parser = argparse.ArgumentParser(description="Index a GitHub repo's PR history")
    parser.add_argument("repo", help="owner/repo format, e.g. vercel/next.js")
    parser.add_argument("--pages", type=int, default=2, help="Pages of 30 PRs each")
    args = parser.parse_args()

    owner, repo = args.repo.split("/")
    print(f"Fetching PRs from {args.repo} ({args.pages * 30} max)...")

    prs = fetch_prs(owner, repo, pages=args.pages)
    print(f"Fetched {len(prs)} PRs. Indexing...")

    count = index_prs(args.repo, prs)
    stats = get_collection_stats(args.repo)

    print(f"Done. {count} documents indexed.")
    print(f"ChromaDB stats: {stats}")

if __name__ == "__main__":
    main()
