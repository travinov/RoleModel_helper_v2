from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Iterable, Mapping

from app.catalog.cache import CatalogBundle
from app.catalog.normalization import (
    normalize_query,
    normalized_tokens,
    parse_department_number,
)


class ToolStatus(str, Enum):
    FOUND = "FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    INVALID = "INVALID"


@dataclass(frozen=True)
class ToolCandidate:
    id: str
    label: str
    score: float
    matched_by: tuple[str, ...] = ()
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    catalog_version: str
    candidates: tuple[ToolCandidate, ...] = ()
    next_required_slots: tuple[str, ...] = ()


def _soft_token_coverage(query: str, candidate: str) -> float:
    query_tokens = normalized_tokens(query)
    candidate_tokens = normalized_tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return 0.0
    matched = 0
    for query_token in query_tokens:
        prefix_length = min(6, max(3, len(query_token) - 2))
        prefix = query_token[:prefix_length]
        if any(
            query_token == candidate_token
            or candidate_token.startswith(prefix)
            or query_token.startswith(candidate_token[:prefix_length])
            for candidate_token in candidate_tokens
        ):
            matched += 1
    return matched / float(len(query_tokens))


def _rank_text(
    query: str,
    name: str,
    aliases: Iterable[Mapping[str, Any]],
) -> tuple[float, tuple[str, ...]]:
    query_normalized = normalize_query(query)
    if not query_normalized:
        return 0.5, ("context_filter",)
    matched_by: list[str] = []
    best = 0.0
    for raw_value, safety in (
        (name, "SAFE"),
        *(
            (str(alias.get("value") or ""), str(alias.get("safety") or "SAFE").upper())
            for alias in aliases
        ),
    ):
        candidate_normalized = normalize_query(raw_value)
        if not candidate_normalized:
            continue
        if query_normalized == candidate_normalized:
            score = 1.0 if safety == "SAFE" else 0.82
            matched_by.append("exact_name" if raw_value == name else "exact_alias")
        else:
            coverage = _soft_token_coverage(query_normalized, candidate_normalized)
            sequence = SequenceMatcher(
                None,
                query_normalized,
                candidate_normalized,
            ).ratio()
            substring = (
                0.9
                if query_normalized in candidate_normalized
                or candidate_normalized in query_normalized
                else 0.0
            )
            token_score = 0.35 + 0.6 * coverage if coverage else 0.0
            score = max(sequence, token_score, substring)
            if coverage:
                matched_by.append("token_coverage")
            if substring:
                matched_by.append("substring")
        if safety == "UNSAFE":
            score = min(score, 0.29)
        elif safety == "AMBIGUOUS":
            score = min(score, 0.82)
        best = max(best, score)
    return round(best, 4), tuple(sorted(set(matched_by)))


class CatalogSearchTools:
    def __init__(self, bundle: CatalogBundle) -> None:
        self._bundle = bundle

    def search_departments(
        self,
        query: str,
        *,
        city: str | None = None,
        position: str | None = None,
        limit: int = 5,
    ) -> ToolResult:
        bounded_limit = self._limit(limit)
        query_number = parse_department_number(query)
        city_normalized = normalize_query(city)
        candidates: list[ToolCandidate] = []
        for department in self._bundle.departments:
            department_number = department.get("number")
            if query_number is not None and department_number != query_number:
                continue
            if (
                city_normalized
                and str(department.get("normalized_city") or "") != city_normalized
            ):
                continue
            score, matched_by = _rank_text(
                query,
                str(department["name"]),
                department.get("aliases") or (),
            )
            if query_number is not None:
                score = max(score, 0.96)
                matched_by = tuple(sorted(set((*matched_by, "exact_number"))))
            if score < 0.30:
                continue
            candidates.append(
                ToolCandidate(
                    id=str(department["id"]),
                    label=str(department["name"]),
                    score=score,
                    matched_by=matched_by,
                    context={
                        "city": str(department.get("city") or ""),
                        "number": department_number,
                    },
                )
            )
        return self._result(candidates, bounded_limit)

    def search_positions(
        self,
        query: str,
        *,
        city: str | None = None,
        department_id: str | None = None,
        limit: int = 5,
    ) -> ToolResult:
        allowed = self._position_ids_for_context(
            city=city,
            department_id=department_id,
        )
        candidates = []
        for position in self._bundle.positions:
            position_id = str(position["id"])
            if allowed is not None and position_id not in allowed:
                continue
            score, matched_by = _rank_text(
                query,
                str(position["name"]),
                position.get("aliases") or (),
            )
            if score < 0.30:
                continue
            candidates.append(
                ToolCandidate(
                    id=position_id,
                    label=str(position["name"]),
                    score=score,
                    matched_by=matched_by,
                )
            )
        return self._result(candidates, self._limit(limit))

    def resolve_profiles(
        self,
        *,
        department_id: str,
        position_id: str,
        city: str | None = None,
        system_id: str | None = None,
        limit: int = 5,
    ) -> ToolResult:
        city_normalized = normalize_query(city)
        candidates = []
        for profile in self._bundle.profiles.values():
            if department_id not in profile.get("department_ids", ()):
                continue
            if position_id not in profile.get("position_ids", ()):
                continue
            if (
                city_normalized
                and str(profile.get("normalized_city") or "") != city_normalized
            ):
                continue
            access = profile.get("access") or ()
            if system_id and not any(
                str(item.get("system_id") or "") == system_id for item in access
            ):
                continue
            candidates.append(
                ToolCandidate(
                    id=str(profile["id"]),
                    label=str(profile["name"]),
                    score=1.0,
                    matched_by=("exact_relation",),
                    context={"city": str(profile.get("city") or "")},
                )
            )
        return self._result(candidates, self._limit(limit))

    def search_systems(
        self,
        query: str | None = None,
        *,
        profile_ids: list[str] | tuple[str, ...] | None = None,
        limit: int = 5,
    ) -> ToolResult:
        allowed = self._system_ids_for_profiles(profile_ids)
        candidates = []
        for system_id, system in self._bundle.systems.items():
            if allowed is not None and system_id not in allowed:
                continue
            score, matched_by = _rank_text(
                query or "",
                str(system["name"]),
                system.get("aliases") or (),
            )
            if query and score < 0.30:
                continue
            candidates.append(
                ToolCandidate(
                    id=system_id,
                    label=str(system["name"]),
                    score=score,
                    matched_by=matched_by,
                )
            )
        return self._result(candidates, self._limit(limit), list_mode=not query)

    def search_roles(
        self,
        *,
        system_id: str,
        profile_ids: list[str] | tuple[str, ...] | None = None,
        query: str | None = None,
        access_level: int | None = None,
        limit: int = 5,
    ) -> ToolResult:
        system = self._bundle.systems.get(system_id)
        if system is None:
            return ToolResult(ToolStatus.INVALID, self._bundle.version)
        allowed = self._role_ids_for_profiles(system_id, profile_ids)
        candidates = []
        for role in system.get("roles") or ():
            role_id = str(role["id"])
            if allowed is not None and role_id not in allowed:
                continue
            if access_level is not None and int(role.get("access_level") or 0) != access_level:
                continue
            score, matched_by = _rank_text(query or "", str(role["name"]), ())
            if query and score < 0.30:
                continue
            candidates.append(
                ToolCandidate(
                    id=role_id,
                    label=str(role["name"]),
                    score=score,
                    matched_by=matched_by,
                    context={
                        "system_id": system_id,
                        "access_level": int(role.get("access_level") or 0),
                    },
                )
            )
        return self._result(candidates, self._limit(limit), list_mode=not query)

    def get_access_instruction(
        self,
        *,
        system_id: str,
        role_ids: list[str] | tuple[str, ...] | None = None,
        limit: int = 5,
    ) -> ToolResult:
        candidates = []
        for instruction in self._bundle.instructions:
            if str(instruction.get("system_id") or "") != system_id:
                continue
            candidates.append(
                ToolCandidate(
                    id=str(instruction["id"]),
                    label=str(instruction.get("title") or instruction["id"]),
                    score=1.0,
                    matched_by=("exact_system_relation",),
                    context={
                        "system_id": system_id,
                        "citation": str(instruction.get("citation") or ""),
                    },
                )
            )
        return self._result(candidates, self._limit(limit), list_mode=True)

    def _position_ids_for_context(
        self,
        *,
        city: str | None,
        department_id: str | None,
    ) -> set[str] | None:
        if not city and not department_id:
            return None
        city_normalized = normalize_query(city)
        allowed: set[str] = set()
        for profile in self._bundle.profiles.values():
            if (
                city_normalized
                and str(profile.get("normalized_city") or "") != city_normalized
            ):
                continue
            if department_id and department_id not in profile.get("department_ids", ()):
                continue
            allowed.update(str(value) for value in profile.get("position_ids", ()))
        return allowed

    def _system_ids_for_profiles(
        self,
        profile_ids: list[str] | tuple[str, ...] | None,
    ) -> set[str] | None:
        if not profile_ids:
            return None
        allowed: set[str] = set()
        for profile_id in profile_ids:
            profile = self._bundle.profiles.get(str(profile_id))
            if profile is None:
                continue
            allowed.update(
                str(item["system_id"]) for item in profile.get("access") or ()
            )
        return allowed

    def _role_ids_for_profiles(
        self,
        system_id: str,
        profile_ids: list[str] | tuple[str, ...] | None,
    ) -> set[str] | None:
        if not profile_ids:
            return None
        allowed: set[str] = set()
        for profile_id in profile_ids:
            profile = self._bundle.profiles.get(str(profile_id))
            if profile is None:
                continue
            for access in profile.get("access") or ():
                if str(access.get("system_id") or "") == system_id:
                    allowed.update(str(value) for value in access.get("role_ids") or ())
        return allowed

    def _result(
        self,
        candidates: list[ToolCandidate],
        limit: int,
        *,
        list_mode: bool = False,
    ) -> ToolResult:
        ranked = sorted(
            candidates,
            key=lambda item: (-item.score, item.label, item.id),
        )[:limit]
        if not ranked:
            status = ToolStatus.NOT_FOUND
        elif len(ranked) == 1 or list_mode:
            status = ToolStatus.FOUND
        else:
            status = ToolStatus.AMBIGUOUS
        return ToolResult(
            status=status,
            catalog_version=self._bundle.version,
            candidates=tuple(ranked),
        )

    @staticmethod
    def _limit(value: int) -> int:
        return max(1, min(int(value), 10))
