"""Storage facade with lightweight cursor access and lazy vector imports."""

from storage.cursor_store import cursor_store


def get_max_pr_number(
    repo_key: str, namespace: str | None = None, temporary: bool = False
) -> int:
    return cursor_store.get_cursor(
        repo_key,
        namespace=namespace,
        storage="temporary" if temporary else "permanent",
    )


def set_max_pr_number(
    repo_key: str,
    last_pr_number: int,
    namespace: str | None = None,
    temporary: bool = False,
):
    cursor_store.set_cursor(
        repo_key,
        last_pr_number,
        namespace=namespace,
        storage="temporary" if temporary else "permanent",
    )


def get_last_updated_at(
    repo_key: str, namespace: str | None = None, temporary: bool = False
) -> str | None:
    return cursor_store.get_updated_at(
        repo_key,
        namespace=namespace,
        storage="temporary" if temporary else "permanent",
    )


def get_refresh_state(
    repo_key: str, namespace: str | None = None, temporary: bool = False
) -> dict[str, str | None]:
    return cursor_store.get_refresh_state(
        repo_key,
        namespace=namespace,
        storage="temporary" if temporary else "permanent",
    )


_VECTOR_EXPORTS = {
    "index_prs",
    "query_similar",
    "list_all_repos",
    "repo_is_indexed_permanently",
    "repo_is_indexed_temporarily",
    "delete_repo_index",
    "get_collection_stats",
    "mark_repo_checked",
    "get_index_epoch",
    "checkpoint_refresh",
    "complete_refresh",
    "IndexInvalidatedError",
}


def __getattr__(name: str):
    """Delay the optional Chroma import until vector storage is actually used."""
    if name in _VECTOR_EXPORTS:
        from storage import vector_store

        return getattr(vector_store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    *_VECTOR_EXPORTS,
    "get_max_pr_number",
    "set_max_pr_number",
    "get_last_updated_at",
    "get_refresh_state",
    "cursor_store",
]
