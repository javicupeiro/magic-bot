"""Settings that control evidence review, repair and context packing."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewPolicy:
    compact_cards: bool = False
    local_repair: bool = False
    prompt_version: str = "v1"
    balanced_context: bool = False

    def __post_init__(self):
        if self.prompt_version not in {"v1", "v2"}:
            raise ValueError("Unsupported review prompt version")


LEGACY_REVIEW_POLICY = ReviewPolicy()
BALANCED_REVIEW_POLICY = ReviewPolicy(
    compact_cards=True, local_repair=True, prompt_version="v2", balanced_context=True)
DEFAULT_REVIEW_POLICY = BALANCED_REVIEW_POLICY


def compact_card(card):
    """Project verified catalogue evidence; keep full records in exchanges.

    Gameplay attributes absent from the normalized card are copied from the same
    first printing that supplies its text. Never trim rules text to fit a budget.
    """
    fields = ("name", "mana_cost", "cmc", "colors", "color_identity", "text")
    result = {key: card[key] for key in fields if key in card}
    printing = next((p for p in card.get("printings", []) if p.get("text") == card.get("text")), {})
    for key in ("type", "types", "subtypes", "supertypes", "power", "toughness", "loyalty", "layout", "names"):
        if key in printing:
            result[key] = printing[key]
        elif key in card:
            result[key] = card[key]
    result["provenance"] = {key: printing[key] for key in ("id", "multiverseid", "set", "number") if key in printing}
    return result
