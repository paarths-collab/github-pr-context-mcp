import requests
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
HEADERS = {
    "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
    "Content-Type": "application/json",
}

# Fetches last N closed PRs with review comments in ONE request
PR_QUERY = """
query GetPRs($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(
      last: 30,
      states: [MERGED, CLOSED],
      before: $cursor,
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo {
        hasPreviousPage
        startCursor
      }
      nodes {
        number
        title
        body
        author { login }
        createdAt
        mergedAt
        additions
        deletions
        files(first: 20) {
          nodes {
            path
            additions
            deletions
            changeType
          }
        }
        reviewThreads(first: 50) {
          nodes {
            isResolved
            path
            line
            comments(first: 10) {
              nodes {
                author { login }
                body
                createdAt
              }
            }
          }
        }
        reviews(first: 20) {
          nodes {
            author { login }
            state
            body
            submittedAt
          }
        }
      }
    }
  }
}
"""

def run_query(query: str, variables: dict) -> dict:
    resp = requests.post(
        GITHUB_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"GraphQL errors: {data['errors']}")
    return data

def fetch_prs(owner: str, repo: str, pages: int = 1) -> list[dict]:
    """Fetch up to pages*30 merged/closed PRs with all review context."""
    all_prs = []
    cursor = None

    for _ in range(pages):
        variables = {"owner": owner, "repo": repo}
        if cursor:
            variables["cursor"] = cursor

        data = run_query(PR_QUERY, variables)
        pr_data = data["data"]["repository"]["pullRequests"]
        nodes = pr_data["nodes"]

        for pr in nodes:
            # Flatten review thread comments
            review_comments = []
            for thread in pr["reviewThreads"]["nodes"]:
                for comment in thread["comments"]["nodes"]:
                    review_comments.append({
                        "file": thread["path"],
                        "line": thread["line"],
                        "resolved": thread["isResolved"],
                        "author": comment["author"]["login"] if comment["author"] else "ghost",
                        "body": comment["body"],
                        "created_at": comment["createdAt"],
                    })

            all_prs.append({
                "number": pr["number"],
                "title": pr["title"],
                "body": pr["body"] or "",
                "author": pr["author"]["login"] if pr["author"] else "ghost",
                "created_at": pr["createdAt"],
                "merged_at": pr["mergedAt"],
                "additions": pr["additions"],
                "deletions": pr["deletions"],
                "files": [
                    {
                        "path": f["path"],
                        "additions": f["additions"],
                        "deletions": f["deletions"],
                        "change_type": f["changeType"],
                    }
                    for f in pr["files"]["nodes"]
                ],
                "review_comments": review_comments,
                "reviews": [
                    {
                        "author": r["author"]["login"] if r["author"] else "ghost",
                        "state": r["state"],
                        "body": r["body"] or "",
                        "submitted_at": r["submittedAt"],
                    }
                    for r in pr["reviews"]["nodes"]
                ],
            })

        page_info = pr_data["pageInfo"]
        if not page_info["hasPreviousPage"]:
            break
        cursor = page_info["startCursor"]

    return all_prs
