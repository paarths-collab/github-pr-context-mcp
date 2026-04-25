import argparse
import re
from fetcher import fetch_prs
from storage import index_prs, get_collection_stats

def main():
    parser = argparse.ArgumentParser(description="Index a GitHub repo's PR history")
    parser.add_argument("repo", help="owner/repo format, e.g. vercel/next.js")
    parser.add_argument("--pages", type=int, default=2, help="Pages of 30 PRs each")
    args = parser.parse_args()

    repo_str = args.repo
    if repo_str.endswith(".git"):
        repo_str = repo_str[:-4]
    
    match = re.search(r'(?:github\.com/)?([^/]+/[^/]+)', repo_str)
    if not match:
        print("❌ Invalid repo format. Use owner/repo or a GitHub URL.")
        return
        
    clean_repo = match.group(1).split('#')[0].split('?')[0]
    owner, repo = clean_repo.split("/")
    print(f"Fetching PRs from {clean_repo} ({args.pages * 30} max)...")

    prs = fetch_prs(owner, repo, pages=args.pages)
    print(f"Fetched {len(prs)} PRs. Indexing...")

    count = index_prs(clean_repo, prs)
    stats = get_collection_stats(clean_repo)

    print(f"Done. {count} documents indexed.")
    print(f"ChromaDB stats: {stats}")

if __name__ == "__main__":
    main()
