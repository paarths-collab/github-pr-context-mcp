# GraphQL query strings only — no HTTP, no transformation logic here.

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
        files(first: 100) {
          nodes {
            path
            additions
            deletions
            changeType
          }
        }
        reviewThreads(first: 100) {
          nodes {
            isResolved
            path
            line
            diffHunk
            comments(first: 50) {
              nodes {
                author { login }
                body
                createdAt
              }
            }
          }
        }
        commits(first: 10) {
          nodes {
            commit {
              message
            }
          }
        }
        reviews(first: 50) {
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
