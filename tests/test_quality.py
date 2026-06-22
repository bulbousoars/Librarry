from librarry.config import QualityConfig
from librarry.quality import ReleaseCandidate, evaluate_release, rank_candidates


def test_rejects_audiobook():
    q = QualityConfig(
        required_extensions=["epub", "mobi"],
        acceptable_extensions=["pdf"],
        reject_extensions=["m4a", "mp3"],
        reject_patterns=["unabr", "audiobook"],
        prefer_patterns=["retail", "epub"],
    )
    score, reason = evaluate_release(
        "C.S.Lewis Out of the Silent Planet Geoffrey Howard PoF m4a",
        "C S Lewis Out of the Silent Planet",
        q,
    )
    assert reason is not None
    assert score == 0.0


def test_prefers_epub():
    q = QualityConfig(
        required_extensions=["epub", "mobi"],
        acceptable_extensions=[],
        reject_extensions=["m4a"],
        reject_patterns=[],
        prefer_patterns=["epub", "retail"],
    )
    ranked = rank_candidates(
        "Doomsday Book",
        "Connie Willis",
        [
            ReleaseCandidate(
                title="Connie.Willis-Doomsday.Book.2005.Retail.EPUB.eBook-BitBook",
                download_url="http://x",
                size_bytes=1,
                indexer="nzbgeek",
                protocol="usenet",
            ),
            ReleaseCandidate(
                title="Connie Willis Doomsday Book audiobook m4b",
                download_url="http://y",
                size_bytes=1,
                indexer="nzbgeek",
                protocol="usenet",
            ),
        ],
        q,
        0.3,
    )
    assert ranked
    assert "EPUB" in ranked[0].title
