from storage.vector_store import (
    index_prs,
    query_similar,
    list_all_repos,
    repo_is_indexed_permanently,
    repo_is_indexed_temporarily,
    delete_repo_index,
    get_collection_stats,
)
from storage.cursor_store import cursor_store

def get_max_pr_number(repo_key: str, namespace: str | None = None) -> int:
    return cursor_store.get_cursor(repo_key, namespace=namespace)

def set_max_pr_number(repo_key: str, last_pr_number: int, namespace: str | None = None):
    cursor_store.set_cursor(repo_key, last_pr_number, namespace=namespace)

__all__ = [
    "index_prs",
    "query_similar",
    "list_all_repos",
    "repo_is_indexed_permanently",
    "repo_is_indexed_temporarily",
    "delete_repo_index",
    "get_collection_stats",
    "get_max_pr_number",
    "set_max_pr_number",
    "cursor_store",
]
