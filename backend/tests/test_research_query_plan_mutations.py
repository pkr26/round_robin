"""Public query grouping respects search limits and approved term coverage."""
import pytest
from round_robin.research.queries import PublicScope, QueryRejected
from round_robin.research.service import query_plan


def test_query_character_limit_accepts_600_and_splits_at_601():
    def scope(final_length):
        return PublicScope(
            terms=tuple((f"term_{i}", "x" * (118 if i < 4 else final_length)) for i in range(5)),
            template_ids=("comparison_v1",),
        )

    exact = query_plan(scope(113))
    assert len(exact) == 1
    assert len(exact[0]["query"]) == 600
    assert exact[0]["term_ids"] == [f"term_{i}" for i in range(5)]
    over = query_plan(scope(114))
    assert [item["term_ids"] for item in over] == [[f"term_{i}" for i in range(4)], ["term_4"]]
    assert all(len(item["query"]) <= 600 for item in over)


def test_query_word_limit_accepts_75_and_rejects_unrepresentable_term():
    scope = PublicScope(terms=(("a", "w " * 35), ("b", "w " * 29)),
                        region="r " * 10, template_ids=("comparison_v1",))
    plan = query_plan(scope)
    assert len(plan) == 1
    assert len(plan[0]["query"].split()) == 75
    assert plan[0]["term_ids"] == ["a", "b"]
    too_many = PublicScope(terms=(("a", "w " * 40),), region="r " * 35,
                           template_ids=("comparison_v1",))
    with pytest.raises(QueryRejected):
        query_plan(too_many)


def test_six_query_limit_preserves_all_groups_and_rejects_seven():
    def scope(count):
        return PublicScope(terms=tuple((f"term_{i}", "w " * 40) for i in range(count)))

    plan = query_plan(scope(6))
    assert len(plan) == 6
    assert [item["term_ids"] for item in plan] == [[f"term_{i}"] for i in range(6)]
    with pytest.raises(QueryRejected):
        query_plan(scope(7))


def test_template_rotation_covers_counterevidence_without_omitting_terms():
    scope = PublicScope(terms=tuple((f"term_{i}", "w " * 40) for i in range(3)))
    plan = query_plan(scope)
    assert [(item["template_id"], item["term_ids"]) for item in plan] == [
        ("comparison_v1", ["term_0"]), ("costs_v1", ["term_1"]),
        ("counterevidence_v1", ["term_2"]), ("costs_v1", ["term_0"]),
        ("counterevidence_v1", ["term_1"]), ("comparison_v1", ["term_2"]),
    ]
