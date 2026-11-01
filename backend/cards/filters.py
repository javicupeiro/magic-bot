"""Validate query arguments and apply filters unsupported by the provider."""

from dataclasses import dataclass
import math
import re

COLOR_NAMES = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}


def color_symbols(values) -> list[str]:
    """Normalize names/symbols to a sorted, unique list drawn from W, U, B, R, G.

    Accept both older provider values such as White and current values such as W.
    An empty input represents a colorless card; unknown values are rejected.
    """
    aliases = {**{key.casefold(): key for key in COLOR_NAMES},
               **{name.casefold(): key for key, name in COLOR_NAMES.items()}}
    try:
        return sorted({aliases[value.casefold()] for value in values})
    except (KeyError, AttributeError) as exc:
        raise ValueError("Colors must be W, U, B, R, G or their English names") from exc


@dataclass(frozen=True)
class CardFilters:
    """Immutable query shared by remote parameter building and local checks.

    None means no restriction. In particular, cmc=0 is a real restriction, so
    cost handling must check for None rather than relying on truthiness.
    """

    name: str | None = None
    language: str | None = None
    subtype: str | None = None
    color: str | None = None
    color_identity: str | None = None
    cmc: str | int | float | None = None

    def cost(self) -> tuple[str, float] | None:
        """Return (operator, threshold), e.g. ('<', 2.0), or None for no cost filter.

        A bare number means equality. Accept only nonnegative finite values and
        the operators supported by our public query contract: =, < and <=.
        """
        if self.cmc is None:
            return None
        match = re.fullmatch(r"(<=|<|=)?\s*(\d+(?:\.\d+)?)", str(self.cmc).strip())
        if not match or not math.isfinite(float(match[2])):
            raise ValueError("cmc must be a nonnegative number, <number or <=number")
        return match[1] or "=", float(match[2])

    def parameters(self) -> dict:
        """Validate all inputs and build the subset safe to send to the provider.

        Identity and unsupported cost comparisons stay local. matches() also
        checks remote filters so provider behavior cannot change our semantics.
        """
        params = {}
        for key, value in (("name", self.name), ("language", self.language), ("subtypes", self.subtype)):
            if value is not None:
                if not value.strip():
                    raise ValueError(f"{key} must not be empty")
                params[key] = value.strip()
        if self.language and not self.name:
            raise ValueError("language requires a name")
        if self.color is not None:
            # The live catalogue uses symbols, despite older examples using names.
            params["colors"] = color_symbols([self.color])[0]
        if self.color_identity is not None:
            # Validate now, before HTTP, even though this filter is applied locally.
            color_symbols([self.color_identity])
        cost = self.cost()
        # The server rejects decimal notation, even 0.0; fractional values stay local.
        if cost is not None and cost[0] == "=" and cost[1].is_integer():
            params["cmc"] = int(cost[1])
        return params

    def matches(self, card: dict, *, exact_name: bool = False) -> bool:
        """Test one original printing before presentation grouping.

        Search names use substring matching; exact resolution requires equality.
        Color and identity mean membership, not mono-color or Commander legality.
        """
        if self.name:
            # An explicit language requires catalogue evidence in foreignNames;
            # never substitute an English name or infer a translated name.
            names = ([entry.get("name", "") for entry in card.get("foreignNames", [])
                      if entry.get("language", "").casefold() == self.language.strip().casefold()]
                     if self.language else [card["name"]])
            query = self.name.strip().casefold()
            if not any(query == name.casefold() if exact_name else query in name.casefold() for name in names):
                return False
        if self.subtype and self.subtype.strip().casefold() not in [s.casefold() for s in card.get("subtypes", [])]:
            return False
        for requested, field in ((self.color, "colors"), (self.color_identity, "colorIdentity")):
            if requested is not None and color_symbols([requested])[0] not in color_symbols(card.get(field, [])):
                return False
        cost = self.cost()
        if cost is not None:
            value = card.get("cmc")
            # Missing numeric data must not accidentally satisfy a zero-cost query.
            if not isinstance(value, (int, float)):
                return False
            operator, threshold = cost
            return {"=": value == threshold, "<": value < threshold, "<=": value <= threshold}[operator]
        return True
