"""근거 키 존재 검증 (결정론, 계획서 §7.4 "존재하지 않는 근거 ID 거부")."""

from __future__ import annotations

from collections.abc import Collection, Iterable


def unknown_evidence_keys(keys: Iterable[str], allowed: Collection[str]) -> set[str]:
    return {key for key in keys if key not in allowed}
