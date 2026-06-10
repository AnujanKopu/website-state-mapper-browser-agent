"""Unit tests for state identity: normalization, hashing, and dedup rules."""

import io

from PIL import Image

from engine.identity import (
    DHASH_MAX_HAMMING,
    SIMHASH_MAX_HAMMING,
    IdentityIndex,
    StateKey,
    action_signature,
    hamming_distance,
    normalize_url,
    screenshot_dhash,
    state_fingerprint,
    strip_positional_selector,
    text_hash,
    text_simhash,
)
from engine.schemas import BoundingBox, Interactable


def _item(selector: str, tag: str = "a") -> Interactable:
    return Interactable(
        selector=selector, tag=tag, bounding_box=BoundingBox(x=0, y=0, width=10, height=10)
    )


def _png(make_pixel) -> bytes:
    img = Image.new("L", (90, 80))
    img.putdata([make_pixel(i % 90, i // 90) for i in range(90 * 80)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
        assert normalize_url("https://a.com/t/9f86d081884c7d65") == "https://a.com/t/:id"

    def test_short_word_segment_untouched(self):
        assert normalize_url("https://a.com/pricing") == "https://a.com/pricing"

    def test_trailing_slash_collapsed(self):
        assert normalize_url("https://a.com/pricing/") == normalize_url("https://a.com/pricing")

    def test_root_path_preserved(self):
        assert normalize_url("https://a.com") == "https://a.com/"


class TestTextHash:
    def test_whitespace_and_case_insensitive(self):
        assert text_hash("Hello   World\n") == text_hash("hello world")

    def test_digits_neutralized(self):
        assert text_hash("Updated 5 minutes ago") == text_hash("Updated 42 minutes ago")

    def test_different_content_differs(self):
        assert text_hash("pricing page") != text_hash("checkout page")


class TestTextSimhash:
    BASE = (
        "FlowState maps your web application into an interactive state graph "
        "covering modals forms auth walls paywalls onboarding flows dashboards "
        "settings billing invitations and checkout funnels for product teams "
    ) * 3

    def test_identical_texts_zero_distance(self):
        assert hamming_distance(text_simhash(self.BASE), text_simhash(self.BASE)) == 0

    def test_small_edit_stays_within_threshold(self):
        edited = self.BASE.replace("onboarding", "registration", 1)
        distance = hamming_distance(text_simhash(self.BASE), text_simhash(edited))
        assert distance <= SIMHASH_MAX_HAMMING

    def test_different_documents_exceed_threshold(self):
        other = (
            "Quarterly revenue dashboards summarize churn retention expansion "
            "and pipeline metrics for finance leadership across regions teams "
        ) * 3
        distance = hamming_distance(text_simhash(self.BASE), text_simhash(other))
        assert distance > SIMHASH_MAX_HAMMING

    def test_timestamps_ignored(self):
        a = self.BASE + " Updated 5 minutes ago"
        b = self.BASE + " Updated 42 minutes ago"
        assert hamming_distance(text_simhash(a), text_simhash(b)) == 0


class TestScreenshotDhash:
    def test_identical_images_zero_distance(self):
        white = _png(lambda x, y: 255)
        assert hamming_distance(screenshot_dhash(white), screenshot_dhash(white)) == 0

    def test_different_images_exceed_threshold(self):
        white = _png(lambda x, y: 255)
        falling_gradient = _png(lambda x, y: 255 - (x * 255 // 89))
        distance = hamming_distance(
            screenshot_dhash(white), screenshot_dhash(falling_gradient)
        )
        assert distance > DHASH_MAX_HAMMING


class TestActionSignature:
    def test_order_and_position_insensitive(self):
        a = [_item("#posts > article:nth-of-type(1) > a"), _item("#nav > a:nth-of-type(2)")]
        b = [_item("#nav > a:nth-of-type(5)"), _item("#posts > article:nth-of-type(3) > a")]
        assert action_signature(a) == action_signature(b)

    def test_new_affordance_changes_signature(self):
        base = [_item("#nav > a")]
        extended = [*base, _item("#close-modal", tag="button")]
        assert action_signature(base) != action_signature(extended)

    def test_strip_positional_selector(self):
        assert (
            strip_positional_selector("#posts > article:nth-of-type(2) > a")
            == "#posts > article > a"
        )


class TestStateFingerprint:
    def test_deterministic(self):
        assert state_fingerprint("https://a.com/p", "skel", "sig") == state_fingerprint(
            "https://a.com/p", "skel", "sig"
        )
        assert len(state_fingerprint("https://a.com/p", "skel", "sig")) == 16

    def test_any_component_distinguishes(self):
        base = state_fingerprint("u", "page", "skel", "sig")
        assert base != state_fingerprint("u2", "page", "skel", "sig")
        assert base != state_fingerprint("u", "modal", "skel", "sig")
        assert base != state_fingerprint("u", "page", "skel2", "sig")
        assert base != state_fingerprint("u", "page", "skel", "sig2")


def _key(**overrides) -> StateKey:
    base = dict(
        url_normalized="https://a.com/p",
        modal_open=False,
        skeleton_hash="skel-A",
        action_sig="sig-A",
        text_simhash=0b1111_0000,
        screenshot_dhash=0b1010,
    )
    base.update(overrides)
    return StateKey(**base)


class TestIdentityIndex:
    def test_exact_match_same_structure_and_affordances(self):
        index = IdentityIndex()
        index.add(_key(), "s1")
        assert index.find(_key()) == "s1"

    def test_text_only_changes_are_the_same_state(self):
        # Same skeleton + same affordances: a timestamp/counter change
        # (wildly different simhash) must NOT create a new state.
        index = IdentityIndex()
        index.add(_key(), "s1")
        assert index.find(_key(text_simhash=0xFFFF_FFFF)) == "s1"

    def test_fuzzy_merge_absorbs_structural_noise(self):
        # Skeleton differs (e.g. ad iframe) but text + pixels + affordances agree.
        index = IdentityIndex()
        index.add(_key(), "s1")
        assert index.find(_key(skeleton_hash="skel-B")) == "s1"

    def test_fuzzy_rejected_when_affordances_differ(self):
        index = IdentityIndex()
        index.add(_key(), "s1")
        assert index.find(_key(skeleton_hash="skel-B", action_sig="sig-B")) is None

    def test_new_affordances_are_a_new_state(self):
        index = IdentityIndex()
        index.add(_key(), "s1")
        assert index.find(_key(action_sig="sig-B")) is None

    def test_modal_flag_separates_states(self):
        index = IdentityIndex()
        index.add(_key(), "s1")
        assert index.find(_key(modal_open=True)) is None

    def test_url_separates_states(self):
        index = IdentityIndex()
        index.add(_key(), "s1")
        assert index.find(_key(url_normalized="https://a.com/other")) is None

    def test_fuzzy_rejected_when_text_too_far(self):
        index = IdentityIndex()
        index.add(_key(), "s1")
        far_simhash = _key().text_simhash ^ 0b0111_1111  # 7 bits flipped > threshold
        assert index.find(_key(skeleton_hash="skel-B", text_simhash=far_simhash)) is None
