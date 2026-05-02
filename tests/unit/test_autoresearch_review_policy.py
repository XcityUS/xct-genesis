from worldseed.autoresearch.handlers.review_paper import _next_status
from worldseed.models.config_schema import SceneConfig
from worldseed.world import WorldEngine


def test_review_policy_waits_for_third_vote_on_split() -> None:
    reviews = [
        {"reviewer": "alex_opt", "verdict": "accept", "tick": 1},
        {"reviewer": "blair_arch", "verdict": "reject", "tick": 2},
    ]

    assert _next_status(reviews, tick=2) == "under_review"


def test_review_policy_waits_for_third_vote_even_with_two_accepts() -> None:
    reviews = [
        {"reviewer": "alex_opt", "verdict": "accept", "tick": 1},
        {"reviewer": "casey_reg", "verdict": "accept", "tick": 2},
    ]

    assert _next_status(reviews, tick=2) == "under_review"


def test_review_policy_uses_three_reviewer_majority() -> None:
    reviews = [
        {"reviewer": "alex_opt", "verdict": "accept", "tick": 1},
        {"reviewer": "blair_arch", "verdict": "reject", "tick": 2},
        {"reviewer": "dana_ac", "verdict": "accept", "tick": 3},
    ]

    assert _next_status(reviews, tick=3) == "accepted"


def test_required_non_visible_entity_enum_without_targets_hides_action() -> None:
    config = SceneConfig.model_validate(
        {
            "scene": {"id": "t", "description": "t"},
            "entities": [
                {
                    "id": "paper_001",
                    "type": "paper",
                    "author": "alex_opt",
                    "status": "accepted",
                    "reviews": [],
                }
            ],
            "agents": [
                {"id": "alex_opt", "location": "research_office", "character": {}},
                {"id": "dana_ac", "location": "research_office", "character": {}},
            ],
            "actions": {
                "review_paper": {
                    "description": "review",
                    "blocks_when_available": True,
                    "params": [
                        {
                            "name": "paper_id",
                            "type": "entity_ref",
                            "required": True,
                            "enum_from": (
                                "entities_of(type='paper', "
                                "where=status=='under_review' and author != $agent "
                                "and not reviewed_by($agent))"
                            ),
                        }
                    ],
                    "preconditions": [],
                    "effects": [],
                }
            },
        }
    )
    engine = WorldEngine(config=config)
    engine.register_from_config()

    assert "review_paper" not in engine._build_action_options("dana_ac")

    engine.state.update_property("paper_001", "status", "under_review")

    assert engine._build_action_options("dana_ac")["review_paper"]["paper_id"] == ["paper_001"]
