"""The role ladder, in the engine (MCP2, D292).

Cloud's roles by name, owner > admin > builder > approver > viewer; cloud maps its `OrgRole` onto these by value.
The self-hosted product has one person, who is the owner.
"""
from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    VIEWER = 1
    #: A viewer who may also answer what waits for a person (AP1, D260).
    APPROVER = 2
    BUILDER = 3
    ADMIN = 4
    OWNER = 5

    @property
    def label(self) -> str:
        return self.name.lower()

    def can_act_as(self, minimum: "Role") -> bool:
        return self >= minimum

    @classmethod
    def parse(cls, value: "str | Role") -> "Role":
        if isinstance(value, Role):
            return value
        try:
            return cls[str(value).upper()]
        except KeyError:
            raise ValueError(f"unknown role {value!r}") from None
