def search(items, query):
    """Return items containing the query (also tolerating trailing whitespace)."""
    matches = []
    for item in items:
        if query in item:
            matches.append(item)
        if query.strip() in item:
            matches.append(item)
    return matches
