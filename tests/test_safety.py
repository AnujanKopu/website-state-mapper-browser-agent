"""Unit tests for the safety rule engine."""

from engine.safety import SafetyCategory, evaluate_action, is_same_origin
from engine.schemas import BoundingBox, Interactable

BASE = "https://demo.test/"


def _item(
    text: str | None = None,
    *,
    tag: str = "button",
    href: str | None = None,
    aria_label: str | None = None,
    in_form: bool = False,
) -> Interactable:
    return Interactable(
        selector="#el",
        tag=tag,
        text=text,
        aria_label=aria_label,
        href=href,
        in_form=in_form,
        bounding_box=BoundingBox(x=0, y=0, width=100, height=30),
    )


def _denied(item: Interactable, category: SafetyCategory) -> None:
    decision = evaluate_action(item, base_url=BASE)
    assert not decision.allowed, f"expected deny, got allow for {item.label!r}"
    assert decision.category == category
    assert decision.reason


def _allowed(item: Interactable) -> None:
    decision = evaluate_action(item, base_url=BASE)
    assert decision.allowed, f"expected allow, denied as {decision.category}: {decision.reason}"


class TestPaymentRules:
    def test_pay_now_button_denied(self):
        _denied(_item("Pay $29 now"), SafetyCategory.PAYMENT)

    def test_place_order_denied(self):
        _denied(_item("Place order"), SafetyCategory.PAYMENT)

    def test_subscribe_denied(self):
        _denied(_item("Subscribe"), SafetyCategory.PAYMENT)

    def test_add_to_cart_denied(self):
        _denied(_item("Add to cart"), SafetyCategory.PAYMENT)

    def test_checkout_link_is_allowed(self):
        # Viewing a checkout page is valuable mapping; submitting it is not.
        _allowed(_item("Checkout", tag="a", href="https://demo.test/checkout"))

    def test_pricing_link_not_confused_with_pay(self):
        _allowed(_item("Pricing", tag="a", href="https://demo.test/pricing"))


class TestDestructiveAndSessionRules:
    def test_delete_denied(self):
        _denied(_item("Delete workspace"), SafetyCategory.DESTRUCTIVE)

    def test_cancel_subscription_denied(self):
        _denied(_item("Cancel subscription"), SafetyCategory.DESTRUCTIVE)

    def test_logout_text_denied(self):
        _denied(_item("Log out", tag="a", href="https://demo.test/bye"), SafetyCategory.SESSION)

    def test_logout_href_denied(self):
        _denied(_item("Exit", tag="a", href="https://demo.test/logout"), SafetyCategory.SESSION)


class TestCommunicationAndPublishRules:
    def test_send_message_denied(self):
        _denied(_item("Send message"), SafetyCategory.COMMUNICATION)

    def test_invite_denied(self):
        _denied(_item("Invite teammates"), SafetyCategory.COMMUNICATION)

    def test_publish_denied(self):
        _denied(_item("Publish"), SafetyCategory.PUBLISH)

    def test_accept_terms_denied(self):
        _denied(_item("Accept terms"), SafetyCategory.LEGAL)

    def test_upload_denied(self):
        _denied(_item("Upload avatar"), SafetyCategory.UPLOAD)


class TestNavigationRules:
    def test_external_origin_denied(self):
        _denied(
            _item("Partner site", tag="a", href="https://example.com/partner"),
            SafetyCategory.EXTERNAL,
        )

    def test_same_origin_allowed(self):
        _allowed(_item("Features", tag="a", href="https://demo.test/features"))

    def test_mailto_denied(self):
        _denied(
            _item("Email sales", tag="a", href="mailto:sales@demo.test"),
            SafetyCategory.CONTACT_PROTOCOL,
        )

    def test_file_download_denied(self):
        _denied(
            _item("Whitepaper", tag="a", href="https://demo.test/whitepaper.pdf"),
            SafetyCategory.DOWNLOAD,
        )

    def test_aria_label_is_checked_too(self):
        _denied(_item(None, aria_label="Delete item"), SafetyCategory.DESTRUCTIVE)

    def test_icon_only_download_is_denied(self):
        item = _item(None)
        item.icon_label = "download"
        _denied(item, SafetyCategory.DOWNLOAD)


class TestFormPolicy:
    def test_form_submit_button_denied(self):
        _denied(_item("Continue", in_form=True), SafetyCategory.FORM_SUBMIT)

    def test_submit_input_denied(self):
        _denied(_item("Go", tag="input", in_form=True), SafetyCategory.FORM_SUBMIT)

    def test_plain_button_outside_form_allowed(self):
        _allowed(_item("Open menu"))

    def test_link_inside_form_allowed(self):
        _allowed(_item("Forgot password?", tag="a", href="https://demo.test/reset", in_form=True))


class TestOrigin:
    def test_same_origin_check(self):
        assert is_same_origin("https://demo.test/a", "https://demo.test/b")
        assert not is_same_origin("https://evil.test/a", "https://demo.test/b")

    def test_www_and_https_redirect_scope_is_allowed(self):
        assert is_same_origin("https://www.youtube.com/", "https://youtube.com/")
        assert is_same_origin("https://demo.test/a", "http://www.demo.test/b")
        assert not is_same_origin("https://studio.demo.test/a", "https://demo.test/b")

    def test_all_file_urls_are_one_origin(self):
        assert is_same_origin("file:///C:/site/a.html", "file:///C:/site/b.html")


async def test_local_probe_guard_aborts_mutations_but_allows_reads():
    from unittest.mock import AsyncMock, MagicMock

    from engine.browser.session import BrowserSession
    from engine.schemas import BrowserConfig

    session = BrowserSession(BrowserConfig())
    session.set_probe_guard(True)
    mutation_route = MagicMock(abort=AsyncMock(), continue_=AsyncMock())
    mutation = MagicMock(url="https://app.test/update", method="POST")

    await session._guard_request(mutation_route, mutation)

    mutation_route.abort.assert_awaited_once_with("blockedbyclient")
    mutation_route.continue_.assert_not_awaited()
    assert session.blocked_mutations == [
        {"method": "POST", "url": "https://app.test/update"}
    ]

    read_route = MagicMock(abort=AsyncMock(), continue_=AsyncMock())
    read = MagicMock(url="https://app.test/data", method="GET")
    await session._guard_request(read_route, read)
    read_route.continue_.assert_awaited_once()

    session.set_probe_guard(True, source_url="https://app.test/page")
    external_route = MagicMock(abort=AsyncMock(), continue_=AsyncMock())
    external = MagicMock(url="https://outside.test/", method="GET")
    external.is_navigation_request.return_value = True
    await session._guard_request(external_route, external)
    external_route.abort.assert_awaited_once_with("blockedbyclient")
    assert session.blocked_probe_navigations[-1]["url"] == "https://outside.test/"
