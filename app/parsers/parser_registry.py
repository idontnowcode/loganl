"""파서 레지스트리. 지정한 프로파일 → 가장 잘 맞는 프로파일 → 원문 파서 순으로 고른다."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from app.models.records import ParserProfile
from app.parsers.auto_parser import AutoParser
from app.parsers.base_parser import LogParser, RawTextParser
from app.parsers.generic_parser import RegexProfileParser

PROFILES_DIR = Path(__file__).resolve().parents[2] / "configs" / "parser_profiles"
logger = logging.getLogger("app.parsers")


class UnknownProfileError(KeyError):
    pass


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers: dict[str, LogParser] = {}
        self.fallback: LogParser = RawTextParser()
        self.load_errors: list[str] = []

    @classmethod
    def default(cls, directory: Path = PROFILES_DIR) -> "ParserRegistry":
        registry = cls()
        if directory.is_dir():
            registry.load_profiles(directory)
        # CR-08 PA-04: 맞는 프로파일이 없을 때 원문 파서 대신 쓸 자동 형식 인식(점수가 프로파일보다 늘 낮다)
        registry.register(AutoParser())
        return registry

    def register(self, parser: LogParser) -> None:
        self._parsers[parser.name] = parser

    def names(self) -> list[str]:
        return sorted(self._parsers)

    def get(self, name: str) -> LogParser:
        if name == self.fallback.name:
            return self.fallback
        try:
            return self._parsers[name]
        except KeyError as exc:
            raise UnknownProfileError(f"파서 프로파일이 없습니다: {name}") from exc

    def load_profiles(self, directory: Path) -> list[str]:
        loaded: list[str] = []
        for path in sorted(directory.glob("*.json")):
            try:
                profile = ParserProfile.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
                self.register(RegexProfileParser(profile))
                loaded.append(profile.name)
            except (OSError, ValueError, ValidationError) as exc:
                message = f"파서 프로파일 {path.name}을(를) 읽지 못했습니다: {exc}"
                self.load_errors.append(message)
                logger.warning(message)
        return loaded

    def select(self, head_lines: Sequence[str], profile: str | None = None) -> LogParser:
        if profile:
            return self.get(profile)
        best: LogParser = self.fallback
        best_score = 0.0
        for name in self.names():
            parser = self._parsers[name]
            score = parser.sniff(head_lines)
            if getattr(getattr(parser, "profile", None), "catch_all", False):
                score *= 0.5  # 포괄형(예: ISO 날짜 + 아무 글)은 구조를 더 읽는 자동 인식에 양보한다(CR-08 PA-04)
            if score > best_score:
                best, best_score = self._parsers[name], score
        return best
