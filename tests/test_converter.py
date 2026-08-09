from pathlib import Path

from mdrk_builder.infrastructure import converter as converter_module
from mdrk_builder.infrastructure.converter import DocumentNormalizer


def test_docx_only_normalizer_does_not_resolve_platform_converter(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"already normalized")

    def fail_if_called():
        raise AssertionError("default_converter must stay lazy for DOCX")

    monkeypatch.setattr(converter_module, "default_converter", fail_if_called)

    with DocumentNormalizer() as normalizer:
        assert normalizer.normalize(source) == source


def test_legacy_converter_is_resolved_once_and_closed(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.doc"
    source.write_bytes(b"legacy")
    calls = {"factory": 0, "close": 0}

    class FakeConverter:
        def convert(self, _source: Path, target: Path) -> Path:
            target.write_bytes(b"normalized")
            return target

        def close(self) -> None:
            calls["close"] += 1

    def factory() -> FakeConverter:
        calls["factory"] += 1
        return FakeConverter()

    monkeypatch.setattr(converter_module, "default_converter", factory)

    with DocumentNormalizer() as normalizer:
        first = normalizer.normalize(source)
        second = normalizer.normalize(source)
        assert first.read_bytes() == b"normalized"
        assert second == first

    assert calls == {"factory": 1, "close": 1}
