from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_render_module():
    path = Path(__file__).parents[1] / "tools" / "render_docx.py"
    spec = importlib.util.spec_from_file_location("mdrk_render_docx", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_renderer_rejects_stale_page_artifacts_before_running_tools(tmp_path) -> None:
    module = _load_render_module()
    source = tmp_path / "sample.docx"
    source.write_bytes(b"fixture")
    output = tmp_path / "render"
    output.mkdir()
    (output / "page-1.png").write_bytes(b"stale")

    with pytest.raises(FileExistsError, match="должен быть свежим"):
        module.render_docx(source, output)
