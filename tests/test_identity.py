"""Unit tests for state identity primitives."""

from engine.identity import normalize_url, state_fingerprint, text_hash


class TestNormalizeUrl:
    def test_strips_fragment(self):
        assert normalize_url("https://a.com/page#section") == "https://a.com/page"

    def test_strips_tracking_params_keeps_real_ones(self):
        url = "https://a.com/p?utm_source=x&plan=pro&fbclid=123"
        assert normalize_url(url) == "https://a.com/p?plan=pro"

    def test_sorts_query_params(self):
        assert normalize_url("https://a.com/?b=2&a=1") == normalize_url("https://a.com/?a=1&b=2")

    def test_lowercases_scheme_and_host_only(self):
        assert normalize_url("HTTPS://A.com/CaseSensitivePath") == (
            "https://a.com/CaseSensitivePath"
        )

    def test_numeric_segment_becomes_id(self):
        assert normalize_url("https://a.com/users/12345/posts") == "https://a.com/users/:id/posts"

    def test_uuid_segment_becomes_id(self):
        url = "https://a.com/orders/123e4567-e89b-12d3-a456-426614174000"
        assert normalize_url(url) == "https://a.com/orders/:id"

    def test_long_hex_segment_becomes_id(self):
        assert normalize_url("https://a.com/t/9f86d081884c7d65" ) == "https://a.com/t/:id"

    def test_short_word_segment_untouched(self):
        assert normalize_url("https://a.com/pricing") == "https://a.com/pricing"

    def test_trailing_slash_collapsed(self):
        assert normalize_url("https://a.com/pricing/") == normalize_url("https://a.com/pricing")

    def test_root_path_preserved(self):
        assert normalize_url("https://a.com") == "https://a.com/"


class TestTextHash:
    def test_whitespace_and_case_insensitive(self):
        assert text_hash("Hello   World\n") == text_hash("hello world")

    def test_different_content_differs(self):
        assert text_hash("pricing page") != text_hash("checkout page")


class TestStateFingerprint:
    def test_deterministic(self):
        a = state_fingerprint("https://a.com/p", text_hash("body"))
        b = state_fingerprint("https://a.com/p", text_hash("body"))
        assert a == b
        assert len(a) == 16

    def test_url_distinguishes_states(self):
        digest = text_hash("same body")
        assert state_fingerprint("https://a.com/p1", digest) != state_fingerprint(
            "https://a.com/p2", digest
        )
