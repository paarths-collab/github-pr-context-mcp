# GraphQL query strings only — no HTTP, no transformation logic here.

PR_QUERY = """
query GetPRs($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(
      first: 30,
      states: [MERGED, CLOSED],
      after: $cursor,
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        number
        title
        body
        author { login }
        createdAt
        updatedAt
        mergedAt
        state
        additions
        deletions
        files(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            path
            additions
            deletions
            changeType
          }
        }
        reviewThreads(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            id
            isResolved
            path
            line
            comments(first: 50) {
              pageInfo { hasNextPage }
              nodes {
                id
                author { login }
                body
                createdAt
                diffHunk
              }
            }
          }
        }
        commits(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            commit {
              oid
              message
            }
          }
        }
        reviews(first: 50) {
          pageInfo { hasNextPage }
          nodes {
            id
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
