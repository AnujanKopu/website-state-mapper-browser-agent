"""Unit tests for action ranking and sibling collapse."""

from engine.ranking import (
    ActionCandidate,
    collapse_siblings,
    detect_surface_families,
    is_auth_entry,
    score_action,
)
from engine.schemas import BoundingBox, Interactable


def _item(
    selector: str,
    *,
    tag: str = "a",
    text: str | None = None,
    href: str | None = None,
    role: str | None = None,
    in_nav: bool = False,
    y: float = 100,
) -> Interactable:
    return Interactable(
        selector=selector,
        tag=tag,
        text=text,
        href=href,
        role=role,
        in_nav=in_nav,
        bounding_box=BoundingBox(x=0, y=y, width=100, height=30),
    )


def _score(item: Interactable, visited: set[str] | None = None) -> float:
    return score_action(ActionCandidate(interactable=item), visited_urls=visited or set())


class TestCollapseSiblings:
    def test_repeated_cards_collapse_to_one_representative(self):
        posts = [
            _item(
                f"#posts > article:nth-of-type({n}) > a",
                text=f"Read post {n}",
                href=f"https://demo.test/post{n}",
            )
            for n in (1, 2, 3)
        ]
        candidates = collapse_siblings(posts)
        assert len(candidates) == 1
        assert candidates[0].collapsed_count == 3
        assert candidates[0].interactable.selector == "#posts > article:nth-of-type(1) > a"
        assert len(candidates[0].grouped_labels) == 3

    def test_nav_links_with_distinct_targets_stay_separate(self):
        nav = [
            _item("body > nav > a:nth-of-type(1)", text="Home", href="https://demo.test/"),
            _item("body > nav > a:nth-of-type(2)", text="Pricing", href="https://demo.test/pricing"),
            _item("body > nav > a:nth-of-type(3)", text="Docs", href="https://demo.test/docs"),
        ]
        assert len(collapse_siblings(nav)) == 3

    def test_slug_family_is_inferred_but_kept_for_bounded_sampling(self):
        profiles = [
            _item(
                f"#profiles > li:nth-of-type({n}) > a",
                text=name,
                href=f"https://demo.test/users/{name.lower()}",
            )
            for n, name in enumerate(("Alice", "Bob", "Carol"), start=1)
        ]

        candidates = collapse_siblings(profiles)

        assert len(candidates) == 3
        assert {candidate.family_pattern for candidate in candidates} == {
            "https://demo.test/users/:param"
        }
        assert len({item.group_id for item in profiles}) == 1
        assert candidates[0].collapsed_count == 3
        assert all(candidate.collapsed_count == 1 for candidate in candidates[1:])

    def test_hrefless_buttons_group_by_text(self):
        tabs = [
            _item("#tabs > button:nth-of-type(1)", tag="button", text="Overview"),
            _item("#tabs > button:nth-of-type(2)", tag="button", text="Integrations"),
        ]
        assert len(collapse_siblings(tabs)) == 2

        repeated = [
            _item("#list > button:nth-of-type(1)", tag="button", text="Expand"),
            _item("#list > button:nth-of-type(2)", tag="button", text="Expand"),
        ]
        collapsed = collapse_siblings(repeated)
        assert len(collapsed) == 1
        assert collapsed[0].collapsed_count == 2

    def test_non_content_routes_do_not_become_dynamic_families(self):
        links = [
            _item("#plan-docs", text="Read the docs", href="https://demo.test/docs"),
            _item("#checkout", text="Checkout", href="https://demo.test/checkout"),
        ]

        candidates = collapse_siblings(links)

        assert len(candidates) == 2
        assert all(candidate.family_pattern is None for candidate in candidates)


class TestSurfaceFamilies:
    def test_repeated_content_routes_are_detected_across_selector_shapes(self):
        links = [
            _item("#hero-game", text="Blade Ball", href="https://demo.test/games/blade-ball"),
            _item("#table-row-1 a", text="Grow a Garden", href="https://demo.test/games/grow-a-garden"),
            _item("#card-news", text="Patch notes", href="https://demo.test/news/patch-notes"),
            _item("#docs", text="Docs", href="https://demo.test/docs"),
        ]

        families = detect_surface_families(links)

        assert {family.kind for family in families} == {"game"}
        assert families[0].discovered_count == 2
        assert families[0].pattern == "https://demo.test/games/:param"


class TestScoreAction:
    def test_flow_keyword_beats_generic_link(self):
        signup = _item("#signup", tag="button", text="Sign up")
        generic = _item("#misc", text="Random article", href="https://demo.test/article")
        assert _score(signup) > _score(generic)

    def test_nav_placement_bonus(self):
        in_nav = _item("#a", text="Team", href="https://demo.test/team", in_nav=True)
        not_in_nav = _item("#b", text="Team", href="https://demo.test/team", in_nav=False)
        assert _score(in_nav) > _score(not_in_nav)

    def test_visited_url_penalized_below_unvisited(self):
        visited_target = _item("#v", text="Guide", href="https://demo.test/guide")
        fresh_target = _item("#f", text="Guide", href="https://demo.test/other")
        visited = {"https://demo.test/guide"}
        assert _score(visited_target, visited) < _score(fresh_target, visited)

    def test_low_value_links_penalized(self):
        privacy = _item("#p", text="Privacy policy", href="https://demo.test/privacy")
        feature = _item("#f", text="Latest updates", href="https://demo.test/updates")
        assert _score(privacy) < _score(feature)

    def test_auth_entry_detected_and_scored_high(self):
        sign_in = _item("#login", text="Sign in", href="https://demo.test/login", in_nav=True)
        generic = _item("#games", text="Top Games", href="https://demo.test/games", in_nav=True)
        assert is_auth_entry(ActionCandidate(interactable=sign_in))
        assert not is_auth_entry(ActionCandidate(interactable=generic))
        assert _score(sign_in) > _score(generic)
