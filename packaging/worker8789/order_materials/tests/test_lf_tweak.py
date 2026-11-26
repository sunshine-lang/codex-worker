import json
import pytest

from lf_text import restore_tweak_newlines
from materials import parse_materials_json


@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize(
    "content",
    [
        "first\n\nsecond\r\nthird\tend",
        'quoted "term" and a literal \\n or \\u000a',
        "\\" * 5 + "\n" + "\\" * 7 + '"' + "\n最后一行",
        "# 客户指南\n\n| 原文 | 译文 |\n|---|---|\n| A | 点击 |",
    ],
)
def test_text_tweak_roundtrip(content, indent):
    materials = {
        "prompt_file": {"filename": "提示.md", "content": content},
        "reference_files": [{"filename": "04-术语.md", "content": content + "\n"}],
    }
    raw = json.dumps(materials, ensure_ascii=False, indent=indent)
    received = raw.replace(r"\n", "\n")  # Langflow _coerce_str_value / unescape_string
    assert restore_tweak_newlines(received) == raw
    assert parse_materials_json(restore_tweak_newlines(received)) == materials
    assert restore_tweak_newlines(raw) == raw
