PR_FIELDS_FRAGMENT = """
fragment PRFields on SearchResultItemConnection {
  nodes {
    ... on PullRequest {
      id
      number
      title
      state
      url
      isDraft
      mergeable
      mergeStateStatus
      author { login }
      repository { nameWithOwner }
      updatedAt
      createdAt
      reviewDecision
      reviewThreads(first: 100) {
        totalCount
        nodes { isResolved }
      }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              state
            }
          }
        }
      }
      reviewRequests(first: 10) {
        nodes {
          requestedReviewer {
            ... on User { login }
            ... on Team { name }
          }
        }
      }
    }
  }
}
"""

SEARCH_MY_PRS = """
query($author_q: String!, $reviewer_q: String!, $assignee_q: String!) {
  authored: search(query: $author_q, type: ISSUE, first: 50) {
    ...PRFields
  }
  reviewing: search(query: $reviewer_q, type: ISSUE, first: 50) {
    ...PRFields
  }
  assigned: search(query: $assignee_q, type: ISSUE, first: 50) {
    ...PRFields
  }
}
""" + PR_FIELDS_FRAGMENT

VIEWER_LOGIN = """
query {
  viewer { login }
}
"""

PR_DETAIL = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      number
      title
      state
      url
      isDraft
      mergeable
      mergeStateStatus
      body
      author { login }
      repository { nameWithOwner }
      createdAt
      updatedAt
      reviewDecision
      reviewThreads(first: 100) {
        totalCount
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(first: 20) {
            nodes {
              author { login }
              body
              createdAt
            }
          }
        }
      }
      comments(first: 100) {
        nodes {
          author { login }
          body
          createdAt
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
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              state
              contexts(first: 50) {
                nodes {
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    detailsUrl
                    startedAt
                    completedAt
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""
