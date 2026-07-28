from storage import query_similar

queries = [
    "string formatting quote normalization",
    "AST node transformation visitor",
    "trailing comma magic trailing comma",
    "line length splitting expression",
    "Python version compatibility target",
    "decorator formatting blank lines",
    "import sorting formatting",
    "comment handling formatting preserved",
    "parentheses bracket wrapping",
    "test case formatter output expected",
]

for q in queries:
    results = query_similar("psf/black", q, n_results=3)
    if results:
        best = results[0]
        snippet = best.get("text", "")[:120]
        print(f"{best['similarity']:.4f}  {q}")
        print(f"        -> {snippet}")
        print()