# GraphQL query strings only — no HTTP, no transformation logic here.
#
# The PR list query is generated per sort order. GitHub's ``orderBy`` accepts
# variables, but the field/direction are fixed per query text here so the
# query stays easy to audit (and so UPDATED_AT callers keep the exact text
# they always had). Supported orders:
#   UPDATED_AT  incremental refresh (newest-updated first, with a since watermark)
#   CREATED_AT  initial window / older-history backfill (created_at never
#               changes, so cursors stay stable between runs)

PR_ORDER_FIELDS = ("UPDATED_AT", "CREATED_AT")

# Fields requested on every PullRequestReviewComment (page one and follow-ups).
_COMMENT_FIELDS = """
                id
                databaseId
                url
                author { __typename login }
                body
                createdAt
                updatedAt
                diffHunk
                originalCommit { oid }
"""

# Fields requested on every PullRequestReviewThread. ``isOutdated`` and
# ``originalLine`` exist on the thread; ``originalCommit``/``diffHunk`` exist
# only on the comment.
_THREAD_FIELDS = """
            id
            isResolved
            isOutdated
            path
            line
            originalLine
            comments(first: 50) {
              totalCount
              pageInfo { hasNextPage endCursor }
              nodes {
__COMMENT_FIELDS__
              }
            }
"""

_PR_QUERY_TEMPLATE = """
query GetPRs($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(
      first: 30,
      states: [MERGED, CLOSED],
      after: $cursor,
      orderBy: {field: __ORDER_FIELD__, direction: DESC}
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
        author { __typename login }
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
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {
__THREAD_FIELDS__
          }
        }
        commits(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            commit {
              oid
              url
              message
              author { name user { login } }
            }
          }
        }
        reviews(first: 50) {
          pageInfo { hasNextPage }
          nodes {
            id
            url
            author { __typename login }
            state
            body
            submittedAt
            updatedAt
          }
        }
      }
    }
  }
}
"""


def _expand(template: str) -> str:
    return (
        template.replace("__THREAD_FIELDS__", _THREAD_FIELDS)
        .replace("__COMMENT_FIELDS__", _COMMENT_FIELDS)
    )


def build_pr_query(order_by: str = "UPDATED_AT") -> str:
    """Return the PR list query ordered DESC by ``order_by``."""
    if order_by not in PR_ORDER_FIELDS:
        raise ValueError(f"order_by must be one of {PR_ORDER_FIELDS}, got {order_by!r}.")
    return _expand(_PR_QUERY_TEMPLATE.replace("__ORDER_FIELD__", order_by))


PR_QUERY = build_pr_query("UPDATED_AT")
PR_QUERY_CREATED_AT = build_pr_query("CREATED_AT")

# Follow-up: remaining comments of one review thread (node id from the PR query).
THREAD_COMMENTS_QUERY = _expand("""
query GetThreadComments($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 50, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
__COMMENT_FIELDS__
        }
      }
    }
  }
}
""")

# Follow-up: remaining review threads of one pull request (node id of the PR).
PR_REVIEW_THREADS_QUERY = _expand("""
query GetPRReviewThreads($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequest {
      reviewThreads(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
__THREAD_FIELDS__
        }
      }
    }
  }
}
""")
