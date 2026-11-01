"""The two model-facing tools; HTTP behavior remains in MtgApiClient.

The factory captures the HTTP client. It is a Python dependency, not an argument
that the model can choose. ToolNode validates each tool's Pydantic schema, while
CardFilters validates domain meanings before the client makes an HTTP request.
"""

from typing import Annotated
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field
from backend.cards.mtg_api import MtgApiClient

Name = Annotated[str, Field(min_length=1, max_length=200)]


class SearchArguments(BaseModel):
    """Model-facing search fields validated before execution.

    null leaves a filter unrestricted. strict=True avoids silent type coercion;
    extra=forbid rejects unknown keys such as URLs or transport controls. CMC
    means numeric mana value, not symbolic printed mana cost. CardFilters checks
    semantic combinations after these JSON-level constraints have passed.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    name: Name | None = None
    language: Name | None = None
    subtype: Name | None = None
    color: Name | None = None
    color_identity: Name | None = None
    cmc: str | int | float | None = None


class NameArguments(BaseModel):
    """Arguments for exact canonical or localized name resolution.

    language selects the provider's localized-name matching. A successful HTTP
    lookup must establish the canonical identity; model memory cannot do so.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    name: Name
    language: Name | None = None



def catalogue_tools(api: MtgApiClient) -> list:
    """Create native LangChain tools bound to one caller-owned HTTP client."""
    @tool(args_schema=SearchArguments)
    def search_cards(**arguments) -> dict:
        """Search live cards by name, language, subtype, color, color_identity or cmc.

        Use English subtype names and W/U/B/R/G colors. cmc accepts 0, <2 or <=2.
        Null means unrestricted. language requires name. Partial results cannot
        establish absence; inspect status, partial and error in the result.
        """
        return api.search_cards(**arguments).to_dict()

    @tool(args_schema=NameArguments)
    def get_card_by_name(**arguments) -> dict:
        """Resolve an exact English or localized name using the live catalogue.

        Use language='Spanish' for a Spanish printed name. Only a complete ok
        result verifies identity. Ambiguous results require user clarification.
        """
        return api.get_card_by_name(**arguments).to_dict()

    return [search_cards, get_card_by_name]
