from fileserver import read_document


def test_reads_public_document():
    assert "public" in read_document("readme.txt")
