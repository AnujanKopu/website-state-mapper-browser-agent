"""Regression coverage for content-agnostic dynamic URL family inference."""

from engine.families import (
    FamilyRegistry,
    infer_template,
    matches_template,
    structure_signature,
)
from engine.schemas import (
    AuthContext,
    BoundingBox,
    Interactable,
    Observation,
    PageSignals,
    PageSnapshot,
)

BOX = BoundingBox(x=0, y=0, width=100, height=30)


def _link(
    selector: str,
    url: str,
    *,
    label: str = "Open",
    region: str = "main",
    in_nav: bool = False,
    container: str = "cards",
    fold: int = 0,
) -> Interactable:
    return Interactable(
        selector=selector,
        tag="a",
        text=label,
        href=url,
        bounding_box=BOX,
        item_id=selector,
        region=region,
        in_nav=in_nav,
        container_key=container,
        fold=fold,
    )


def _observe(
    url: str,
    *,
    skeleton: str = "html body main article h1 p a",
    interactables: list[Interactable] | None = None,
    signals: PageSignals | None = None,
) -> Observation:
    return Observation(
        snapshot=PageSnapshot(
            url=url,
            title="Detail",
            visible_text="Detail content",
            html="",
            screenshot_png=b"png",
            dom_skeleton=skeleton,
            signals=signals or PageSignals(),
        ),
        interactables=interactables or [],
        url_normalized=url,
        text_digest="text",
        text_simhash=1,
        skeleton_hash="skeleton",
        action_sig="actions",
        screenshot_dhash=1,
        fingerprint=url,
        auth_context=AuthContext.GUEST,
    )


def _surface(
    registry: FamilyRegistry,
    items: list[Interactable],
    *,
    source: str = "source",
) -> list:
    return registry.observe_surface(
        source_key=source,
        source_structure=f"structure-{source}",
        base_url="https://example.test/",
        items=items,
    )


def test_hashtag_cohort_promotes_without_known_content_words():
    registry = FamilyRegistry()
    values = ["edit", "dogwithablog", "film", "movie"]
    items = [
        _link(
            f"#rail > a:nth-of-type({index})",
            f"https://example.test/hashtag/{value}/shorts",
            label=value,
        )
        for index, value in enumerate(values, 1)
    ]

    families = _surface(registry, items)

    assert len(families) == 1
    assert families[0].pattern == "https://example.test/hashtag/:param/shorts"
    assert families[0].status == "provisional"
    assert families[0].payload()["kind"] == "items"
    assert len(families[0].sample_targets) == 3


def test_single_short_and_same_url_substates_are_not_a_family():
    registry = FamilyRegistry()
    repeated = [
        _link(
            f"#menu-{index}",
            "https://example.test/shorts/1CiNJwkp01M",
            label=f"Menu {index}",
        )
        for index in range(12)
    ]

    assert _surface(registry, repeated) == []
    assert registry.candidates == {}


def test_query_value_family_accumulates_across_mixed_sources():
    registry = FamilyRegistry()
    for index, value in enumerate(("a1", "b2", "c3", "d4", "e5")):
        _surface(
            registry,
            [_link(f"#unique-{index}", f"https://example.test/watch?v={value}")],
            source=f"state-{index}",
        )

    family = registry.family_for_url("https://example.test/watch?v=c3")
    assert family is not None
    assert family.pattern == "https://example.test/watch?v=:param"
    assert len(family.support_sources) == 5


def test_static_same_depth_navigation_routes_do_not_promote():
    registry = FamilyRegistry()
    items = [
        _link(
            f"nav > a:nth-of-type({index})",
            f"https://example.test/{name}",
            label=name,
            region="nav",
            in_nav=True,
        )
        for index, name in enumerate(("about", "pricing", "docs", "contact", "legal"), 1)
    ]

    assert _surface(registry, items) == []


def test_bare_one_segment_usernames_require_strong_container_evidence():
    weak = FamilyRegistry()
    for index, name in enumerate(("alice", "bob", "carol", "dora", "evan")):
        _surface(
            weak,
            [_link(f"#mixed-{index}", f"https://example.test/{name}")],
            source=f"state-{index}",
        )
    assert weak.family_for_url("https://example.test/alice") is None

    strong = FamilyRegistry()
    items = [
        _link(
            f"#people > a:nth-of-type({index})",
            f"https://example.test/{name}",
        )
        for index, name in enumerate(("alice", "bob", "carol"), 1)
    ]
    assert _surface(strong, items)[0].pattern == "https://example.test/:param"


def test_optional_trailing_slug_and_unicode_values_are_generic():
    urls = [
        "https://example.test/x/101/alpha",
        "https://example.test/x/202/%D8%B4%D8%A7%D9%84%D9%8A%D8%A9",
        "https://example.test/x/opaque-303/third",
        "https://example.test/x/404",
    ]
    inferred = infer_template(urls, optional_tail=True)

    assert inferred is not None
    pattern, slots = inferred
    assert pattern == "https://example.test/x/:param/:optional"
    assert slots == ["path:1", "path:2?"]
    assert all(matches_template(url, pattern) for url in urls)


def test_optional_inference_does_not_absorb_an_ancestor_back_link():
    registry = FamilyRegistry()
    items = [
        _link(
            f"#people > a:nth-of-type({index})",
            f"file:///site/profiles/{name}.html",
        )
        for index, name in enumerate(("alice", "bob", "carol"), 1)
    ]
    _surface(registry, items)
    for index, name in enumerate(("alice", "bob", "carol"), 1):
        registry.observe_surface(
            source_key=f"profile-{index}",
            source_structure="profile",
            base_url=f"file:///site/profiles/{name}.html",
            items=[_link("#back", "file:///site/directory.html")],
        )

    family = registry.family_for_url("file:///site/profiles/alice.html")
    assert family is not None
    assert family.pattern == "file:///site/profiles/:param"
    assert registry.family_for_url("file:///site/directory.html") is None


def test_three_samples_required_and_layout_variant_stays_sampled():
    registry = FamilyRegistry()
    items = [
        _link(
            f"#items > a:nth-of-type({index})",
            f"https://example.test/entry/{value}",
            label=value,
        )
        for index, value in enumerate(("alpha", "bravo", "moderator"), 1)
    ]
    family = _surface(registry, items)[0]

    registry.record_sample(family, family.sample_targets[0], _observe(family.sample_targets[0]))
    _, status = registry.record_sample(
        family, family.sample_targets[1], _observe(family.sample_targets[1])
    )
    assert status == "provisional"

    variant = family.sample_targets[2]
    _, status = registry.record_sample(
        family,
        variant,
        _observe(
            variant,
            skeleton="html body main article h1 p form input button aside nav a",
            interactables=[_link("#moderate", variant)],
            signals=PageSignals(form_count=1),
        ),
    )
    assert status == "confirmed"
    assert len(family.samples) == 3


def test_compatible_collection_prefix_classifies_routes_as_page_variants():
    registry = FamilyRegistry()
    source = _observe("https://example.test/games")
    items = [
        _link(
            f"#categories > a:nth-of-type({index})",
            f"https://example.test/games/{value}",
            label=value,
        )
        for index, value in enumerate(("trending", "new", "popular"), 1)
    ]
    family = registry.observe_surface(
        source_key=f"{source.snapshot.url}|source",
        source_structure=source.skeleton_hash,
        source_signature=structure_signature(source),
        base_url=source.snapshot.url,
        items=items,
    )[0]

    for url in family.sample_targets:
        registry.record_sample(family, url, _observe(url))

    assert family.status == "confirmed"
    assert family.family_kind == "collection_variant_family"
    assert family.collection_anchor_urls == {"https://example.test/games"}
    assert family.payload()["family_kind"] == "collection_variant_family"


def test_conflicting_samples_reject_instead_of_authorizing_skips():
    registry = FamilyRegistry(validation_cap=3)
    items = [
        _link(
            f"#mixed > a:nth-of-type({index})",
            f"https://example.test/mixed/{value}",
        )
        for index, value in enumerate(("one", "two", "three"), 1)
    ]
    family = _surface(registry, items)[0]
    observations = [
        _observe(family.sample_targets[0], skeleton="html body main article"),
        _observe(
            family.sample_targets[1],
            skeleton="html body form input button",
            signals=PageSignals(form_count=1),
        ),
        _observe(
            family.sample_targets[2],
            skeleton="html body dialog nav ul li a",
            signals=PageSignals(modal_open=True),
        ),
    ]
    for url, observation in zip(family.sample_targets, observations, strict=True):
        registry.record_sample(family, url, observation)

    assert family.status == "rejected"
    assert family.skipped_urls == set()
