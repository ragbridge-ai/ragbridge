"""Access groups: the names a document is labelled with, and a request is scoped by.

A group is a short name such as ``public`` or ``hr``. A document carries any number of
them; a scoped request carries the groups its caller belongs to, and sees only the
documents sharing at least one (docs/plans/chat-roadmap.md, decisions 1 and 2).
"""

from collections.abc import Iterable
from typing import Annotated

from pydantic import Field, StringConstraints

MAX_GROUPS = 32
"""The most groups one document or one request may carry."""

GROUP_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"

AccessGroup = Annotated[str, StringConstraints(pattern=GROUP_PATTERN)]
"""1-64 ASCII letters, digits or ``. _ : -``, starting with a letter or digit.

ASCII only, like an external id, so two names that look alike cannot differ by Unicode
form and end up as two groups.
"""

AccessGroups = Annotated[list[AccessGroup], Field(max_length=MAX_GROUPS)]
"""A request's or a document's list of groups, as the API accepts it."""


def normalise_scope(groups: Iterable[str] | None) -> list[str] | None:
    """A request's ``access_groups`` as retrieval takes them.

    ``None`` stays ``None``: the caller sent no groups and is not limited. An empty list
    stays an empty list: a caller with no groups, who sees nothing. The two are different
    requests and must never be mixed up, in retrieval or in the answer cache.
    """
    return None if groups is None else normalise_groups(groups)


def normalise_groups(groups: Iterable[str]) -> list[str]:
    """``groups`` without duplicates, sorted.

    Two lists with the same groups in another order are the same scope: they must
    share an answer-cache entry and must not count as a change when a document is
    written again.
    """
    return sorted(set(groups))
