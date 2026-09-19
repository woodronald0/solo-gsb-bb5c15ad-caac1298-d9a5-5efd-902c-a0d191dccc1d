"""POST /documents/{id}/reverts 撤销修订端到端集成测试（每个用例独立临时数据库）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _create(client, content, doc_id="cfg", author=None):
    body = {"id": doc_id, "content": content}
    if author is not None:
        body["author"] = author
    r = client.post("/documents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _patch(client, doc_id, base_revision, request_id, patch, author=None):
    body = {"base_revision": base_revision, "request_id": request_id, "patch": patch}
    if author is not None:
        body["author"] = author
    return client.patch(f"/documents/{doc_id}", json=body)


def _revert(client, doc_id, revision, request_id, author=None):
    body = {"revision": revision, "request_id": request_id}
    if author is not None:
        body["author"] = author
    return client.post(f"/documents/{doc_id}/reverts", json=body)


# ---------------- 基本撤销（目标即当前修订） ----------------


def test_revert_latest_reversion_restores_previous_snapshot(client):
    _create(client, {"a": 1, "b": 2})
    assert _patch(client, "cfg", 1, "req-2", {"a": 10}).status_code == 200

    r = _revert(client, "cfg", 2, "undo-2", author="mallory")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 3
    assert body["reverted_revision"] == 2
    assert body["base_revision"] == 2
    assert body["request_id"] == "undo-2"
    assert body["replayed"] is False
    assert body["changed"] is True
    assert body["idempotent"] is False
    # 内容还原为 rev1 快照
    assert body["content"] == {"a": 1, "b": 2}
    assert client.get("/documents/cfg").json()["content"] == {"a": 1, "b": 2}


def test_revert_produced_revision_is_normal_traceable_revision(client):
    _create(client, {"a": 1, "b": 2}, author="alice")
    _patch(client, "cfg", 1, "req-2", {"a": 10})
    assert _revert(client, "cfg", 2, "undo-2", author="bob").status_code == 200

    # 撤销修订是修订历史中的普通条目，字段完整可追溯
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [(h["revision"], h["request_id"], h["author"]) for h in hist] == [
        (1, None, "alice"),
        (2, "req-2", None),
        (3, "undo-2", "bob"),
    ]
    detail = client.get("/documents/cfg/revisions/3").json()
    # patch 记录的是逆向补丁（a 改回 1），base_revision 指向被撤销修订
    assert detail["patch"] == {"a": 1}
    assert detail["base_revision"] == 2
    assert detail["replayed"] is False
    assert detail["author"] == "bob"
    assert detail["content"] == {"a": 1, "b": 2}


def test_revert_restores_key_deleted_by_target_revision(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "req-del", {"a": None})  # rev2 删除 a
    r = _revert(client, "cfg", 2, "undo-del")
    assert r.status_code == 200
    assert r.json()["content"] == {"a": 1, "b": 2}


def test_revert_deletes_key_added_by_target_revision(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "req-add", {"c": 3})  # rev2 新增 c
    r = _revert(client, "cfg", 2, "undo-add")
    assert r.status_code == 200
    assert r.json()["content"] == {"a": 1}


def test_revert_array_change_replaces_whole_array(client):
    _create(client, {"items": [1, 2, 3]})
    _patch(client, "cfg", 1, "req-arr", {"items": [9, 9]})
    r = _revert(client, "cfg", 2, "undo-arr")
    assert r.status_code == 200
    assert r.json()["content"] == {"items": [1, 2, 3]}


def test_revert_non_object_root_document(client):
    _create(client, [1, 2, 3])
    _patch(client, "cfg", 1, "req-root", [4])
    r = _revert(client, "cfg", 2, "undo-root")
    assert r.status_code == 200
    assert r.json()["content"] == [1, 2, 3]


# ---------------- 撤销旧修订：三方合并保留后续其他路径修改 ----------------


def test_revert_old_revision_keeps_later_disjoint_changes(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "rev2", {"a": 10})       # rev2 改 a
    _patch(client, "cfg", 2, "rev3", {"c": 3})        # rev3 新增 c（其他路径）
    r = _revert(client, "cfg", 2, "undo-rev2")
    assert r.status_code == 200, r.text
    body = r.json()
    # 目标修订的变更被撤销（a 还原），rev3 在其他路径的修改保留
    assert body["content"] == {"a": 1, "b": 2, "c": 3}
    assert body["revision"] == 4
    assert body["replayed"] is True
    rev4 = client.get("/documents/cfg/revisions/4").json()
    assert rev4["base_revision"] == 2
    assert rev4["replayed"] is True
    assert rev4["patch"] == {"a": 1}


def test_revert_old_revision_nested_disjoint_changes_merge(client):
    _create(client, {"db": {"host": "h", "port": 5432}})
    _patch(client, "cfg", 1, "rev2", {"db": {"port": 6543}})     # rev2 改 port
    _patch(client, "cfg", 2, "rev3", {"db": {"ssl": True}})      # rev3 加 ssl
    r = _revert(client, "cfg", 2, "undo-rev2")
    assert r.status_code == 200
    assert r.json()["content"] == {"db": {"host": "h", "port": 5432, "ssl": True}}


def test_revert_when_content_already_equivalent_is_noop(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "rev2", {"a": 2})   # rev2: a=2
    _patch(client, "cfg", 2, "rev3", {"a": 1})   # rev3 又手动改回 a=1
    # 撤销 rev2 的逆向补丁（{a:1}）三方重放后与当前版相同 → 不产生新修订
    r = _revert(client, "cfg", 2, "undo-noop")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False
    assert body["revision"] == 3
    assert body["replayed"] is True
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2, 3]


# ---------------- 撤销冲突：409 revert_conflict 且不写入 ----------------


def test_revert_conflict_returns_pointer_and_three_values(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "rev2", {"a": 10})   # rev2: a=10
    _patch(client, "cfg", 2, "rev3", {"a": 20})   # rev3 又把 a 改成 20
    before = client.get("/documents/cfg").json()

    r = _revert(client, "cfg", 2, "undo-conflict")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revert_conflict"
    assert detail["document_id"] == "cfg"
    assert detail["request_id"] == "undo-conflict"
    assert detail["reverted_revision"] == 2
    assert detail["base_revision"] == 2
    assert detail["current_revision"] == 3
    conflict = detail["conflicts"][0]
    assert conflict["path"] == "/a"
    # base=目标快照值，current=当前值，theirs=撤销想还原的旧值
    assert conflict["values"] == {"base": 10, "current": 20, "theirs": 1}
    assert conflict["missing"] == []

    # 不写新修订，文档保持 rev3
    after = client.get("/documents/cfg").json()
    assert after == before
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2, 3]


def test_revert_conflict_nested_path_pointer(client):
    _create(client, {"db": {"port": 5432}})
    _patch(client, "cfg", 1, "rev2", {"db": {"port": 6543}})
    _patch(client, "cfg", 2, "rev3", {"db": {"port": 3306}})
    r = _revert(client, "cfg", 2, "undo")
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["path"] == "/db/port"


def test_revert_conflict_does_not_consume_request_id(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "rev2", {"a": 10})
    _patch(client, "cfg", 2, "rev3", {"a": 20})
    # 撤销 rev2 冲突
    assert _revert(client, "cfg", 2, "undo-retry").status_code == 409
    # request_id 未被占用：改撤不存在分歧的 rev3（a 还原为 rev2 的 10）即可成功
    r = _revert(client, "cfg", 3, "undo-retry")
    assert r.status_code == 200, r.text
    assert r.json()["content"] == {"a": 10}


# ---------------- 目标修订校验 ----------------


def test_revert_revision_one_is_422(client):
    _create(client, {"a": 1})
    r = _revert(client, "cfg", 1, "undo-1")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_revert_revision"
    assert detail["revision"] == 1
    assert detail["current_revision"] == 1
    # 不占用 request_id，不产生修订
    assert client.get("/documents/cfg").json()["revision"] == 1


def test_revert_unknown_revision_is_422(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "rev2", {"a": 2})
    r = _revert(client, "cfg", 99, "undo-x")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_revert_revision"
    assert r.json()["detail"]["revision"] == 99


def test_revert_zero_revision_rejected_by_model(client):
    _create(client, {"a": 1})
    r = client.post("/documents/cfg/reverts", json={"revision": 0, "request_id": "x"})
    assert r.status_code == 422


def test_revert_missing_document_is_404(client):
    assert _revert(client, "ghost", 2, "undo").status_code == 404


def test_revert_extra_field_rejected(client):
    _create(client, {"a": 1})
    r = client.post(
        "/documents/cfg/reverts",
        json={"revision": 1, "request_id": "x", "bogus": 1},
    )
    assert r.status_code == 422


# ---------------- 幂等 ----------------


def test_revert_same_request_id_is_idempotent(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "rev2", {"a": 10})
    r1 = _revert(client, "cfg", 2, "undo-idem")
    assert r1.status_code == 200
    first = r1.json()
    # 再发一次相同请求
    r2 = _revert(client, "cfg", 2, "undo-idem")
    assert r2.status_code == 200
    second = r2.json()
    assert second["idempotent"] is True
    assert second["revision"] == first["revision"] == 3
    assert second["content"] == first["content"] == {"a": 1, "b": 2}
    # 没有额外修订
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2, 3]


def test_revert_same_request_id_different_target_is_409(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "rev2", {"a": 2})
    _patch(client, "cfg", 2, "rev3", {"c": 3})   # 其他路径，保证撤销 rev2 可重放
    assert _revert(client, "cfg", 2, "dup").status_code == 200
    # 同 request_id 改撤 rev3 → 指纹不同
    r = _revert(client, "cfg", 3, "dup")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"


def test_revert_request_id_shared_with_matching_patch_replays(client):
    # 撤销在语义上等价于“以目标修订为基线提交逆向补丁”：
    # 若同一 (base_revision, patch) 已由普通 PATCH 提交过，同 request_id 直接重放。
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "rev2", {"a": 10})
    r1 = _patch(client, "cfg", 2, "shared", {"a": 1})  # 手工把 a 改回 1
    assert r1.status_code == 200
    # 撤销 rev2 计算出的指纹与上面的 PATCH 完全一致 → 幂等返回 rev3
    r2 = _revert(client, "cfg", 2, "shared")
    assert r2.status_code == 200
    body = r2.json()
    assert body["idempotent"] is True
    assert body["revision"] == 3
    assert body["content"] == {"a": 1, "b": 2}
    assert client.get("/documents/cfg/revisions").json()["items"][-1]["revision"] == 3


def test_revert_request_id_colliding_with_different_patch_is_409(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "rev2", {"a": 10})
    assert _patch(client, "cfg", 2, "used", {"a": 77}).status_code == 200
    # 撤销 rev2 的逆向补丁是 {a:1}，与已登记的 {a:77} 不同 → 409
    r = _revert(client, "cfg", 2, "used")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"


# ---------------- 契约校验 ----------------


SCHEMA = {
    "type": "object",
    "properties": {
        "a": {"type": "integer", "minimum": 0},
        "b": {"type": "string"},
    },
    "required": ["a"],
    "additionalProperties": False,
}


def _set_schema(client, schema):
    return client.put("/documents/cfg/schema", json={"schema": schema})


def test_revert_validated_against_current_contract_success(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, SCHEMA).status_code == 200
    assert _patch(client, "cfg", 1, "rev2", {"a": 5}).status_code == 200
    r = _revert(client, "cfg", 2, "undo-ok")
    assert r.status_code == 200
    assert r.json()["content"] == {"a": 1, "b": "x"}


def test_revert_violating_contract_is_422_and_writes_nothing(client):
    # 契约设置之前的历史可以包含如今不合法的值：
    # rev1 的 a 是字符串，rev2 把它修正为整数并在 rev2 之后设置严格契约，
    # rev3 只在不相交路径加 b（契约允许）。
    _create(client, {"a": "oops"})
    _patch(client, "cfg", 1, "rev2", {"a": 1})
    assert _set_schema(client, SCHEMA).status_code == 200
    _patch(client, "cfg", 2, "rev3", {"b": "y"})
    before = client.get("/documents/cfg").json()

    # 撤销 rev2 想把 a 还原为 "oops"：路径可三方重放（b 保留），
    # 但最终候选不满足契约 → 422
    r = _revert(client, "cfg", 2, "undo-bad")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "schema_validation_failed"
    assert detail["current_revision"] == 3
    assert any(e["path"] == "/a" and e["validator"] == "type" for e in detail["errors"])

    # 回滚：不写修订、不占用 request_id
    assert client.get("/documents/cfg").json() == before
    r2 = _revert(client, "cfg", 3, "undo-bad")  # 撤销 rev3（删除 b）合法
    assert r2.status_code == 200, r2.text
    assert r2.json()["content"] == {"a": 1}


def test_revert_conflict_takes_precedence_over_contract(client):
    # 契约设置之前 rev1 的 a 为负值（如今不合法），rev2 修正为 10；
    # 设置严格契约（minimum: 0）后 rev3 又把 a 改成 20（合法但与 rev2 不同）。
    _create(client, {"a": -5})
    _patch(client, "cfg", 1, "rev2", {"a": 10})
    assert _set_schema(client, SCHEMA).status_code == 200
    _patch(client, "cfg", 2, "rev3", {"a": 20})
    # 撤销 rev2 想把 a 还原为 -5（违反 minimum），且三方在 /a 上各不相同
    # （base=10 / current=20 / theirs=-5）→ 先冲突 → 409 revert_conflict，
    # 优先于契约校验。
    r = _revert(client, "cfg", 2, "undo")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revert_conflict"
    c = detail["conflicts"][0]
    assert c["path"] == "/a"
    assert c["values"] == {"base": 10, "current": 20, "theirs": -5}
