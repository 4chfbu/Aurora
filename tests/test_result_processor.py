import pytest

from aurora.services.flag_validator import FlagValidator
from aurora.services.result_processor import ResultProcessor


@pytest.mark.parametrize(
    "value",
    [
        "flag{*****}",
        "flag{abc***}",
        "flag{...}",
        "flag{a\tb}",
        "flag{a\u0001b}",
        "flag{a\ufffdb}",
        "flag{a\u200bb}",
        "flag{a\ufdd0b}",
    ],
)
def test_flag_validator_rejects_masked_and_unreadable_payloads(value: str) -> None:
    assert not FlagValidator.is_valid_flag_value(value)


@pytest.mark.parametrize("value", ["flag{readable-value_42?}", "qwxf{\u4e2d\u6587}"])
def test_flag_validator_accepts_printable_payloads(value: str) -> None:
    assert FlagValidator.is_valid_flag_value(value)


def test_result_processor_normalizes_model_confidence_labels() -> None:
    processor = ResultProcessor()

    assert processor._confidence("high") == 0.8
    assert processor._confidence("VERY HIGH") == 0.95
    assert processor._confidence("unknown") == 0.5
    assert processor._confidence(4) == 1.0
    assert processor._confidence(float("nan")) == 0.5


def test_result_processor_normalizes_priority_and_ignores_malformed_entries() -> None:
    processor = ResultProcessor()

    assert processor._priority("high") == 0.8
    assert processor._priority("unexpected") == 0.5
    assert processor._objects([{"statement": "valid"}, "invalid", None]) == [{"statement": "valid"}]
    assert processor._objects("not-a-list") == []
