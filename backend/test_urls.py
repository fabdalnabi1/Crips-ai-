"""Basic YouTube URL validation tests (requires backend dependencies)."""
from fastapi import HTTPException
from backend.app import validate_youtube_url

def test_urls():
    id = "7wTJfzQ-pQ8"
    for url in (
        f"https://youtu.be/{id}?si=abc",
        f"https://www.youtube.com/watch?v={id}",
        f"https://www.youtube.com/live/{id}?si=abc",
        f"https://www.youtube.com/shorts/{id}",
    ):
        assert validate_youtube_url(url) == f"https://www.youtube.com/watch?v={id}"
    for url in ("https://example.com/", "http://youtu.be/" + id, "https://youtu.be/not-valid"):
        try:
            validate_youtube_url(url)
            raise AssertionError("Invalid URL accepted")
        except HTTPException:
            pass

if __name__ == "__main__":
    test_urls()
    print("URL validation checks passed")
