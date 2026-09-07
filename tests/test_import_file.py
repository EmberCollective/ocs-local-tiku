"""文件导入：JSON/ZIP 解析、容量护栏、分批 upsert；上传走 multipart。"""

import io
import json
import zipfile

import pytest

from app.api.import_api import ImportFileError, extract_import_items


def _item(question="题目", answer="甲") -> dict:
    return {"question": question, "type": "single", "options": "A. 甲\nB. 乙", "answer": answer}


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


# ---------- 纯解析：extract_import_items ----------


def test_extract_plain_json_object_with_items():
    items = extract_import_items(_json_bytes({"items": [_item()]}))
    assert items == [_item()]


def test_extract_plain_json_bare_array():
    items = extract_import_items(_json_bytes([_item(), _item(question="第二题")]))
    assert len(items) == 2


def test_extract_tolerates_bom():
    data = b"\xef\xbb\xbf" + _json_bytes({"items": [_item()]})
    assert extract_import_items(data) == [_item()]


def test_extract_tolerates_raw_newline_inside_string():
    """手改文件常见：字符串里混入裸换行（非法 JSON）也能解析。"""
    data = b'[{"question":"a\nb","answer":"\xe7\x94\xb2"}]'
    assert extract_import_items(data) == [{"question": "a\nb", "answer": "甲"}]


def test_extract_rejects_invalid_json():
    with pytest.raises(ImportFileError, match="JSON"):
        extract_import_items(b"not json at all")


def test_extract_rejects_non_list_payload():
    with pytest.raises(ImportFileError, match="格式"):
        extract_import_items(_json_bytes({"foo": "bar"}))


def test_extract_zip_merges_json_entries_and_ignores_others():
    archive = _zip_bytes(
        {
            "a.json": _json_bytes({"items": [_item()]}),
            "nested/b.json": _json_bytes([_item(question="第二题")]),
            "README.txt": b"ignore me",
        }
    )
    items = extract_import_items(archive, filename="backup.zip")
    assert [item["question"] for item in items] == ["题目", "第二题"]


def test_extract_zip_without_json_entries_rejected():
    with pytest.raises(ImportFileError, match="JSON"):
        extract_import_items(_zip_bytes({"README.txt": b"no json"}))


def test_extract_zip_entry_broken_json_rejected_with_filename():
    archive = _zip_bytes({"a.json": b"{"})
    with pytest.raises(ImportFileError, match="a.json"):
        extract_import_items(archive)


def test_extract_filename_suffix_wins_for_zip():
    """文件名 .zip 但内容不是 zip → 明确报错而不是当 JSON 解析。"""
    with pytest.raises(ImportFileError, match="ZIP"):
        extract_import_items(_json_bytes({"items": []}), filename="backup.zip")


def test_extract_zip_entry_over_uncompressed_cap(monkeypatch):
    """zip 炸弹护栏：条目声明解压尺寸超上限直接拒绝，不实际解压。"""
    from app.api import import_api

    monkeypatch.setattr(import_api, "MAX_ENTRY_UNCOMPRESSED_BYTES", 4)
    archive = _zip_bytes({"a.json": _json_bytes({"items": [_item()]})})
    with pytest.raises(ImportFileError, match="解压"):
        extract_import_items(archive)


# ---------- API 层 /api/import/file ----------


async def test_api_import_file_json(client):
    resp = await client.post(
        "/api/import/file",
        files={"file": ("backup.json", _json_bytes({"items": [_item()]}), "application/json")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"code": 1, "imported": 1, "updated": 0, "skipped": 0}
    row = await client.app.state.db.query_one(
        "SELECT source FROM questions WHERE question = ?", ("题目",)
    )
    assert row["source"] == "import"


async def test_api_import_file_zip(client):
    archive = _zip_bytes(
        {
            "a.json": _json_bytes({"items": [_item(question="第一题")]}),
            "b.json": _json_bytes({"items": [_item(question="第二题"), _item(question="第三题")]}),
        }
    )
    resp = await client.post(
        "/api/import/file",
        files={"file": ("backup.zip", archive, "application/zip")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"code": 1, "imported": 3, "updated": 0, "skipped": 0}

    again = await client.post(
        "/api/import/file",
        files={"file": ("backup.zip", archive, "application/zip")},
    )
    assert again.json() == {"code": 1, "imported": 0, "updated": 3, "skipped": 0}


async def test_api_import_file_empty_items_rejected(client):
    resp = await client.post(
        "/api/import/file",
        files={"file": ("empty.json", _json_bytes({"items": []}), "application/json")},
    )
    assert resp.status_code == 400
    assert (await client.app.state.db.query_one("SELECT COUNT(*) AS n FROM questions"))["n"] == 0


async def test_api_import_file_broken_content_rejected(client):
    resp = await client.post(
        "/api/import/file",
        files={"file": ("bad.json", b"{{{", "application/json")},
    )
    assert resp.status_code == 400


async def test_api_import_file_over_5000_chunked(client):
    """5001 条跨 5000 上限自动分批，全部入库且只记一条导入日志。"""
    items = [_item(question=f"题目 {i}", answer="甲") for i in range(5001)]
    resp = await client.post(
        "/api/import/file",
        files={"file": ("big.json", _json_bytes({"items": items}), "application/json")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"code": 1, "imported": 5001, "updated": 0, "skipped": 0}
    db = client.app.state.db
    assert (await db.query_one("SELECT COUNT(*) AS n FROM questions"))["n"] == 5001
    assert (
        await db.query_one("SELECT COUNT(*) AS n FROM call_log WHERE kind = 'import'")
    )["n"] == 1
