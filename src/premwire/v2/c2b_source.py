from __future__ import annotations

import re
from dataclasses import dataclass

from .c2b_models import SourceMetadata, TextSpan
from .resolution import SourceRegistry
from .stage_c1_evaluation import EvaluationManifestItem


SOURCE_METADATA_VERSION = "1.0"
TEXT_NORMALIZER_VERSION = "1.0"
_QUOTE_MAP = {
    "“": '"',
    "”": '"',
    "„": '"',
    "«": '"',
    "»": '"',
    "’": "'",
    "‘": "'",
}
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CTA_RE = re.compile(
    r"\b(subscribe|follow us|join us|подписывайтесь|подпишись|"
    r"присоединяйтесь|переходите по ссылке)\b",
    re.IGNORECASE,
)
_FOOTER_RE = re.compile(
    r"^\s*(?:source|источник)\s*:\s*.+$|^\s*@[\w.-]+\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF]"
)


class SourceMetadataBuilder:
    def __init__(self, registry: SourceRegistry) -> None:
        self.registry = registry

    def build(self, item: EvaluationManifestItem) -> SourceMetadata:
        record = self.registry.resolve(item.source_platform, item.registry_account)
        if record is None:
            raise ValueError(
                f"source is not registered: "
                f"{item.source_platform}/{item.registry_account}"
            )
        return SourceMetadata(
            story_id=item.story_id,
            source_event_id=item.source_event_id,
            platform=item.source_platform,
            source_account=item.source_account,
            canonical_source_name=record.canonical_name,
            source_role=record.source_role,
            trust_tier=record.trust_tier,
            url=item.source_url,
            published_at=item.published_at,
            original_language=item.source_language,
            original_text=item.source_text,
        )


@dataclass(frozen=True, slots=True)
class NormalizedText:
    original_text: str
    normalized_text: str
    normalized_to_original: tuple[int, ...]
    annotations: tuple[TextSpan, ...]
    version: str = TEXT_NORMALIZER_VERSION

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        if start < 0 or end <= start or end > len(self.normalized_text):
            raise ValueError("invalid normalized span")
        return (
            self.normalized_to_original[start],
            self.normalized_to_original[end - 1] + 1,
        )


class SourceTextNormalizer:
    def normalize(self, original_text: str) -> NormalizedText:
        normalized: list[str] = []
        mapping: list[int] = []
        previous_was_space = False
        for original_index, char in enumerate(original_text):
            replacement = _QUOTE_MAP.get(char, char)
            if replacement.isspace():
                if previous_was_space:
                    continue
                normalized.append(" ")
                mapping.append(original_index)
                previous_was_space = True
                continue
            normalized.append(replacement)
            mapping.append(original_index)
            previous_was_space = False
        annotations = []
        for span_type, pattern in (
            ("url", _URL_RE),
            ("advertising_cta", _CTA_RE),
            ("source_footer", _FOOTER_RE),
            ("emoji", _EMOJI_RE),
        ):
            for match in pattern.finditer(original_text):
                annotations.append(
                    TextSpan(
                        span_type=span_type,
                        source_text=match.group(0),
                        start_offset=match.start(),
                        end_offset=match.end(),
                    )
                )
        annotations.sort(key=lambda span: (span.start_offset, span.end_offset))
        return NormalizedText(
            original_text=original_text,
            normalized_text="".join(normalized),
            normalized_to_original=tuple(mapping),
            annotations=tuple(annotations),
        )
