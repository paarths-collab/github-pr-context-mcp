# GraphQL query strings only — no HTTP, no transformation logic here.

# Must match the ``first:`` argument of ``pullRequests`` below; the indexer uses
# it to report the configured PR limit (pages * PRS_PER_PAGE).
PRS_PER_PAGE = 30

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
        url
        title
        body
        author { login __typename }
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
                url
                author { login __typename }
                body
                createdAt
                diffHunk
                pullRequestReview { url }
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
            url
            author { login __typename }
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
