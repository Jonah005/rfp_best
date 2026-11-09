# ruff: noqa: S314 - parsing SVG this module itself produced, to prove it is well formed
"""A diagram is data the platform checks and draws, not a picture a model made."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.services.diagram_spec import (
    SpecError,
    describe_changes,
    layout,
    parse_spec,
    parse_style,
    readable_on,
    render,
    unconfirmed_elements,
)

FLOW = {
    "direction": "LR",
    "nodes": [
        {"id": "a", "label": "Customer requests a test", "kind": "start_end", "lane": "Customer"},
        {"id": "b", "label": "Scope confirmed", "lane": "PMO"},
        {"id": "c", "label": "Scope agreed?", "kind": "decision", "lane": "PMO"},
        {"id": "d", "label": "Run the tests", "lane": "Security"},
        {"id": "e", "label": "Retest", "lane": "Security", "basis": "ASSUMPTION"},
        {"id": "f", "label": "Report", "lane": "PMO"},
    ],
    "edges": [
        {"source": "a", "target": "b"},
        {"source": "b", "target": "c"},
        {"source": "c", "target": "d", "label": "yes"},
        {"source": "c", "target": "b", "label": "no"},
        {"source": "d", "target": "e"},
        {"source": "e", "target": "f"},
        {"source": "a", "target": "f"},
    ],
}


def test_a_link_to_a_box_that_does_not_exist_is_refused_with_a_reason():
    bad = {"nodes": [{"id": "a", "label": "A"}], "edges": [{"source": "a", "target": "zz"}]}
    with pytest.raises(SpecError, match="not a box"):
        parse_spec(bad)


def test_duplicate_ids_a_self_link_and_an_oversized_diagram_are_refused():
    with pytest.raises(SpecError, match="unique"):
        parse_spec({"nodes": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]})
    with pytest.raises(SpecError, match="itself"):
        parse_spec(
            {"nodes": [{"id": "a", "label": "A"}], "edges": [{"source": "a", "target": "a"}]}
        )
    with pytest.raises(SpecError):
        parse_spec({"nodes": [{"id": f"n{i}", "label": "x"} for i in range(41)]})
    with pytest.raises(SpecError):
        parse_spec({"nodes": [{"id": "bad id!", "label": "x"}]})


def test_every_drawing_is_well_formed_svg_for_every_direction_and_theme():
    for direction in ("LR", "TB"):
        for theme in ("CLEAN", "BLUEPRINT", "MONO", "WARM"):
            spec = parse_spec({**FLOW, "direction": direction})
            svg = render("FLOW", spec, parse_style({"theme": theme}), title="Test")
            root = ET.fromstring(svg)  # raises if the markup is broken
            assert root.tag.endswith("svg")


def test_words_in_a_label_cannot_break_out_of_the_drawing():
    spec = parse_spec(
        {"nodes": [{"id": "a", "label": '<script>alert(1)</script> & "quotes"'}], "edges": []}
    )
    svg = render("FLOW", spec)
    assert "<script>" not in svg
    ET.fromstring(svg)


def test_boxes_in_the_same_lane_and_column_do_not_overlap():
    spec = parse_spec(
        {
            "nodes": [
                {"id": "s", "label": "Start", "lane": "L"},
                *[{"id": f"x{i}", "label": f"Parallel step {i}", "lane": "L"} for i in range(4)],
            ],
            "edges": [{"source": "s", "target": f"x{i}"} for i in range(4)],
        }
    )
    laid = layout(spec)
    boxes = list(laid.boxes.values())
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            separate = a.x + a.w <= b.x or b.x + b.w <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y
            assert separate, f"{a.node.id} overlaps {b.node.id}"


def test_a_cycle_is_drawn_rather_than_looping_forever():
    spec = parse_spec(
        {
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        }
    )
    ET.fromstring(render("FLOW", spec))


def test_swimlanes_appear_only_when_boxes_name_a_lane():
    with_lanes = layout(parse_spec(FLOW))
    assert [name for name, *_ in with_lanes.lanes] == ["Customer", "PMO", "Security"]
    plain = layout(parse_spec({"nodes": [{"id": "a", "label": "A"}]}))
    assert plain.lanes == []


def test_a_sequence_diagram_lists_messages_in_the_order_given():
    spec = parse_spec(
        {
            "nodes": [{"id": "c", "label": "Customer"}, {"id": "p", "label": "Portal"}],
            "edges": [
                {"source": "c", "target": "p", "label": "First"},
                {"source": "p", "target": "c", "label": "Second", "dashed": True},
            ],
        }
    )
    svg = render("SEQUENCE", spec)
    ET.fromstring(svg)
    assert svg.index("First") < svg.index("Second")


def test_a_style_is_a_closed_set_of_values():
    assert parse_style({"theme": "BLUEPRINT", "corner": 2}).theme == "BLUEPRINT"
    with pytest.raises(SpecError):
        parse_style({"theme": "NEON"})
    with pytest.raises(SpecError):
        parse_style({"node_fill": "red; background:url(x)"})
    with pytest.raises(SpecError):
        parse_style({"corner": 400})
    assert parse_style({"node_fill": "#AABBCC"}).node_fill == "#aabbcc"


def test_text_stays_readable_on_a_chosen_fill():
    assert readable_on("#ffffff") == "#111111"
    assert readable_on("#0e2a47") == "#ffffff"


def test_changes_are_worked_out_from_the_structures():
    old = parse_spec(FLOW)
    new_raw = {
        **FLOW,
        "nodes": [n for n in FLOW["nodes"] if n["id"] != "e"]
        + [{"id": "g", "label": "Executive review", "lane": "PMO"}],
        "edges": [e for e in FLOW["edges"] if "e" not in (e["source"], e["target"])],
    }
    lines = describe_changes(old, parse_spec(new_raw))
    assert any("Added box: Executive review" in line for line in lines)
    assert any("Removed box: Retest" in line for line in lines)
    assert describe_changes(old, old) == ["No change to the boxes or links."]


def test_assumptions_and_proposals_are_listed_for_a_reviewer():
    found = unconfirmed_elements(parse_spec(FLOW))
    assert found == [{"kind": "box", "label": "Retest", "basis": "ASSUMPTION"}]


# --- neatness ---------------------------------------------------------------------------


def _wide(n_nodes: int, extra_links: int = 0) -> dict:
    nodes = [{"id": f"n{i}", "label": f"Step {i}", "lane": f"L{i % 3}"} for i in range(n_nodes)]
    edges = [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(n_nodes - 1)]
    edges += [{"source": "n0", "target": f"n{i + 3}"} for i in range(extra_links)]
    return {"nodes": nodes, "edges": edges}


def test_a_small_chain_is_readable_and_a_crowded_one_says_why():
    from app.services.diagram_spec import readability

    assert readability(parse_spec(_wide(8)))["issues"] == []
    crowded = readability(parse_spec(_wide(22, extra_links=7)))
    assert crowded["issues"]
    assert any("boxes" in i for i in crowded["issues"])
    assert any("jump over" in i for i in crowded["issues"])


def test_a_question_that_does_not_branch_is_drawn_as_a_step():
    from app.services.diagram_spec import tidy

    spec = parse_spec(
        {
            "nodes": [
                {"id": "a", "label": "Submit"},
                {"id": "b", "label": "Admin approval", "kind": "decision"},
                {"id": "c", "label": "Notify"},
            ],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        }
    )
    fixed, notes = tidy(spec)
    assert fixed.nodes[1].kind == "process" and notes


def test_a_question_that_branches_stays_a_diamond():
    from app.services.diagram_spec import tidy

    spec = parse_spec(
        {
            "nodes": [
                {"id": "a", "label": "Approved?", "kind": "decision"},
                {"id": "b", "label": "Yes path"},
                {"id": "c", "label": "No path"},
            ],
            "edges": [
                {"source": "a", "target": "b", "label": "yes"},
                {"source": "a", "target": "c", "label": "no"},
            ],
        }
    )
    assert tidy(spec)[0].nodes[0].kind == "decision"


def test_link_labels_fit_in_the_gap_and_are_kept_short():
    from app.services.diagram_spec import MAX_EDGE_LABEL, layout, tidy

    spec = parse_spec(
        {
            "nodes": [{"id": "a", "label": "One"}, {"id": "b", "label": "Two"}],
            "edges": [{"source": "a", "target": "b", "label": "x" * 60}],
        }
    )
    spec, _ = tidy(spec)
    assert len(spec.edges[0].label) <= MAX_EDGE_LABEL
    laid = layout(spec)
    a, b = laid.boxes["a"], laid.boxes["b"]
    gap = b.x - (a.x + a.w)
    assert gap >= len(spec.edges[0].label) * 6.4


def test_an_entry_box_sits_beside_what_it_feeds_not_at_the_far_left():
    from app.services.diagram_spec import layout

    spec = parse_spec(
        {
            "nodes": [{"id": n, "label": n.upper()} for n in ("a", "b", "c", "d", "x")],
            "edges": [
                {"source": "a", "target": "b"},
                {"source": "b", "target": "c"},
                {"source": "c", "target": "d"},
                {"source": "x", "target": "d"},
            ],
        }
    )
    laid = layout(spec)
    # x feeds d, so it sits in the column before d rather than the first column.
    assert laid.boxes["x"].x > laid.boxes["a"].x


def test_the_drawing_is_never_wider_than_its_content():
    from app.services.diagram_spec import layout

    spec = parse_spec(FLOW)
    laid = layout(spec)
    right_most = max(b.x + b.w for b in laid.boxes.values())
    assert laid.width >= right_most
    assert laid.width - right_most < 200
