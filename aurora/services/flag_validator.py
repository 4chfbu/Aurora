from __future__ import annotations

import re
import unicodedata

from sqlmodel import Session

from aurora.models import Artifact
from aurora.services.artifact_store import ArtifactStore


# CTF flags frequently use an event-specific prefix which does not contain
# either "flag" or "ctf" (for example ``qwxf{...}``).  A useful candidate is
# therefore an identifier-like prefix followed by one non-empty brace pair.
# Requiring at least two prefix characters and at least one ASCII letter keeps
# this from treating ordinary JSON/object syntax as a flag.
# The payload is narrowed here as a first-pass scan filter.  The semantic
# readability checks in ``is_valid_flag_value`` remain the source of truth for
# values supplied directly by a Worker.
FLAG_VALUE_PATTERN = r"[a-z0-9][a-z0-9_-]{1,63}\{[^\s{}*]{1,200}\}"
FLAG_PATTERNS = [re.compile(rf"(?i)(?<![a-z0-9_-]){FLAG_VALUE_PATTERN}")]

TRUSTED_FLAG_ORIGINS = {"challenge_input", "target_observation", "operator_observation", "verified_derivation"}
TRUSTED_LEGACY_FLAG_TYPES = {"imported_attachment", "browser-inspection", "browser-interaction", "flag-verification"}

DECOY_PAYLOAD_MARKERS = (
    "fake",
    "false",
    "decoy",
    "dummy",
    "bogus",
    "wrong",
    "invalid",
    "placeholder",
    "not_real",
    "not-real",
    "notreal",
    "not_the_flag",
    "not-the-flag",
    "notflag",
    "test_flag",
    "example_flag",
    "假flag",
    "假_flag",
    "假旗",
    "诱饵",
    "錯誤",
    "错误",
    "不是真",
    "不是flag",
)


class FlagValidator:
    def __init__(self, artifact_store: ArtifactStore | None = None) -> None:
        self.artifact_store = artifact_store or ArtifactStore()

    def extract_candidate_flags(self, session: Session, *, artifact_refs: list[str], project_id: str | None = None) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for artifact_id in artifact_refs:
            artifact = session.get(Artifact, artifact_id)
            if artifact is not None and project_id is not None and artifact.project_id != project_id:
                continue
            if not self.is_trusted_evidence_artifact(artifact):
                continue
            content = self.artifact_store.read_text(artifact, max_bytes=128_000)
            content = self._scannable_content(artifact, content)
            if not content:
                continue
            for pattern in FLAG_PATTERNS:
                for match in pattern.finditer(content):
                    value = match.group(0)
                    if not self.is_valid_flag_value(value):
                        continue
                    key = value.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append({
                        "value": value,
                        "artifact_ref": artifact_id,
                        "validator": "replay" if artifact.origin_kind == "verified_derivation" else "observed",
                    })
        return candidates

    def is_verified_candidate(self, session: Session, *, value: str, artifact_ref: str | None, project_id: str | None = None) -> bool:
        """Return whether a proposed flag is present in trusted artifact evidence."""
        if not artifact_ref or not self.is_valid_flag_value(value):
            return False
        artifact = session.get(Artifact, artifact_ref)
        if artifact is None or (project_id is not None and artifact.project_id != project_id):
            return False
        return any(
            candidate["value"].lower() == value.lower()
            for candidate in self.extract_candidate_flags(session, artifact_refs=[artifact_ref], project_id=project_id)
        )

    def is_trusted_artifact_ref(self, session: Session, artifact_ref: str | None, project_id: str | None = None) -> bool:
        """Return whether an artifact id can be used as provenance for a derived flag."""
        artifact = session.get(Artifact, artifact_ref) if artifact_ref else None
        return bool(artifact and (project_id is None or artifact.project_id == project_id) and self.is_trusted_evidence_artifact(artifact))

    @staticmethod
    def is_valid_flag_value(value: str) -> bool:
        normalized_value = value.strip()
        match = re.fullmatch(rf"(?i){FLAG_VALUE_PATTERN}", normalized_value)
        if match is None:
            return False
        prefix, payload = normalized_value.split("{", 1)
        if not any(character.isalpha() and character.isascii() for character in prefix):
            return False
        payload = payload[:-1]
        if not FlagValidator._is_readable_payload(payload):
            return False
        payload = payload.lower()
        # Output schemas frequently contain flag{...}; it is never evidence.
        return "..." not in payload and payload not in {"example", "your_flag", "flag", "ctf", "xxx"}

    @staticmethod
    def _is_readable_payload(payload: str) -> bool:
        """Reject masked, invisible, or binary/undecodable flag payloads.

        Artifact text is decoded with replacement semantics, so malformed
        bytes become U+FFFD (``\ufffd``).  That code point is printable according
        to Python, but it is evidence that the original content was not valid
        readable text and must not make a flag candidate valid.
        """
        def is_noncharacter(character: str) -> bool:
            codepoint = ord(character)
            return 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFF in (0xFFFE, 0xFFFF)

        return bool(payload) and all(
            character.isprintable()
            and not character.isspace()
            and character not in "{}*"
            and character != "\ufffd"
            and not unicodedata.category(character).startswith("C")
            and not is_noncharacter(character)
            for character in payload
        )

    @staticmethod
    def is_decoy_flag_value(value: str) -> bool:
        """Return whether the brace payload explicitly labels itself a fake."""
        if not FlagValidator.is_valid_flag_value(value):
            return False
        payload = value.strip().split("{", 1)[1][:-1].lower()
        compact = re.sub(r"[\s_-]+", "", payload)
        return any(marker in payload or re.sub(r"[\s_-]+", "", marker) in compact for marker in DECOY_PAYLOAD_MARKERS)

    @staticmethod
    def is_trusted_evidence_artifact(artifact: Artifact | None) -> bool:
        return bool(
            artifact is not None
            and artifact.sensitivity != "secret"
            and (
                artifact.origin_kind in TRUSTED_FLAG_ORIGINS
                or (artifact.origin_kind == "unclassified" and artifact.type in TRUSTED_LEGACY_FLAG_TYPES)
            )
        )

    def _scannable_content(self, artifact: Artifact, content: str) -> str:
        if "[stdout]" in content:
            after_stdout = content.split("[stdout]", 1)[1]
            return after_stdout.split("[stderr]", 1)[0]
        return content
