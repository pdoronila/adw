import os

DOCS_ROOT = os.path.join(os.path.dirname(__file__), "docs")


def read_document(name):
    """Return the contents of a document by name from DOCS_ROOT."""
    path = os.path.join(DOCS_ROOT, name)
    with open(path) as f:
        return f.read()
