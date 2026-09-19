"""三方合并 / Merge Patch 算法单元测试。"""
from __future__ import annotations

from app.merge import (
    MISSING,
    apply_merge_patch,
    present_conflict,
    reverse_merge_patch,
    three_way_merge,
)


# ---------------- RFC 7386 apply_merge_patch ----------------


def test_patch_null_deletes_key():
    assert apply_merge_patch({"a": "b", "c": 1}, {"a": None}) == {"c": 1}


def test_patch_nested_merge():
    target = {"a": {"x": 1, "y": 2}, "keep": True}
    patch = {"a": {"x": 9, "z": 3}, "new": {"n": 0}}
    assert apply_merge_patch(target, patch) == {
        "a": {"x": 9, "y": 2, "z": 3},
        "keep": True,
        "new": {"n": 0},
    }


def test_patch_non_object_target_replaced_then_patched():
    # RFC 7386: target 非对象时视为空对象
    assert apply_merge_patch([1, 2], {"a": "b"}) == {"a": "b"}
    assert apply_merge_patch("scalar", {"a": None}) == {}


def test_patch_non_object_patch_replaces_whole_doc():
    assert apply_merge_patch({"a": {"b": 1}}, [1, 2, 3]) == [1, 2, 3]
    assert apply_merge_patch({"a": 1}, None) is None
    assert apply_merge_patch({"a": 1}, "whole") == "whole"
    assert apply_merge_patch({"a": 1}, 42) == 42
    assert apply_merge_patch({"a": 1}, True) is True


def test_patch_null_value_inside_key_is_delete_not_store():
    # {"a": null} 必须删除 a，而不是存成 null
    result = apply_merge_patch({"a": {"b": 1}, "c": None}, {"a": None})
    assert result == {"c": None}
    assert "a" not in result
    assert result["c"] is None  # 真实 null 保留


def test_patch_does_not_mutate_inputs():
    target = {"a": {"b": 1}}
    patch = {"a": {"c": 2}}
    apply_merge_patch(target, patch)
    assert target == {"a": {"b": 1}}
    assert patch == {"a": {"c": 2}}


def test_patch_empty_patch_is_noop():
    assert apply_merge_patch({"a": 1}, {}) == {"a": 1}


# ---------------- three_way_merge ----------------


def _merge(base, current, theirs):
    return three_way_merge(base, current, theirs)


def test_no_concurrent_changes_takes_theirs():
    base = {"a": 1, "b": 2}
    current = {"a": 1, "b": 2}
    theirs = {"a": 1, "b": 99}
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {"a": 1, "b": 99}
    assert conflicts == []


def test_disjoint_paths_auto_replay():
    base = {"a": 1, "b": 2}
    current = {"a": 1, "b": 2, "c": 3}          # 服务端加 c
    theirs = {"a": 9, "b": 2, "c": 3}           # 来稿改 a（基于旧基线打补丁后的形态）
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {"a": 9, "b": 2, "c": 3}
    assert conflicts == []


def test_same_result_both_sides_no_conflict():
    base = {"a": 1}
    current = {"a": 5}
    theirs = {"a": 5}
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {"a": 5}
    assert conflicts == []


def test_diverging_path_conflicts_and_lists_everything():
    base = {"a": 1, "b": 2, "c": 3}
    current = {"a": 10, "b": 2, "c": 30}
    theirs = {"a": 100, "b": 2, "c": 30}
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {"b": 2, "c": 30}
    assert "a" not in merged
    paths = sorted(c["path"] for c in conflicts)
    assert paths == ["/a"]
    c = conflicts[0]
    assert (c["base"], c["current"], c["theirs"]) == (1, 10, 100)


def test_multiple_conflicts_collected():
    base = {"a": 1, "b": 1, "c": 1}
    current = {"a": 2, "b": 2, "c": 1}
    theirs = {"a": 3, "b": 3, "c": 1}
    merged, conflicts = _merge(base, current, theirs)
    assert sorted(c["path"] for c in conflicts) == ["/a", "/b"]
    # 合并结果不完整（冲突键被省略），调用方必须丢弃
    assert merged == {"c": 1}


def test_nested_disjoint_changes_merge_recursively():
    base = {"db": {"host": "h", "port": 5432}, "flag": False}
    current = {"db": {"host": "h", "port": 5432, "ssl": True}, "flag": False}
    theirs = {"db": {"host": "newhost", "port": 5432}, "flag": False}
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {
        "db": {"host": "newhost", "port": 5432, "ssl": True},
        "flag": False,
    }
    assert conflicts == []


def test_nested_diverging_conflict_path_is_json_pointer():
    base = {"db": {"pool": {"size": 10}}}
    current = {"db": {"pool": {"size": 20}}}
    theirs = {"db": {"pool": {"size": 30}}}
    merged, conflicts = _merge(base, current, theirs)
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == "/db/pool/size"
    assert (conflicts[0]["base"], conflicts[0]["current"], conflicts[0]["theirs"]) == (10, 20, 30)


def test_deletion_vs_edit_is_conflict():
    # 服务端删除 a，来稿把 a 改成新值 → 冲突
    base = {"a": 1, "b": 2}
    current = {"b": 2}
    theirs = {"a": 9, "b": 2}
    merged, conflicts = _merge(base, current, theirs)
    assert [c["path"] for c in conflicts] == ["/a"]
    assert conflicts[0]["base"] == 1
    assert conflicts[0]["current"] is MISSING
    assert conflicts[0]["theirs"] == 9
    presented = present_conflict(conflicts[0])
    assert presented["values"] == {"base": 1, "current": None, "theirs": 9}
    assert presented["missing"] == ["current"]


def test_both_delete_is_resolved():
    base = {"a": 1, "b": 2}
    current = {"b": 2}
    theirs = {"b": 2}
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {"b": 2}
    assert conflicts == []
    assert "a" not in merged


def test_one_side_delete_other_untouched_takes_delete():
    base = {"a": 1, "b": 2}
    current = {"b": 2}                    # 服务端删 a
    theirs = {"a": 1, "b": 2, "c": 3}     # 来稿加 c（没动 a）
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {"b": 2, "c": 3}
    assert conflicts == []


def test_arrays_are_replaced_not_index_merged():
    base = {"items": [1, 2, 3]}
    current = {"items": [1, 2, 3, 4]}
    theirs = {"items": [9, 2, 3]}
    merged, conflicts = _merge(base, current, theirs)
    assert [c["path"] for c in conflicts] == ["/items"]


def test_type_mismatch_edit_vs_edit_conflicts():
    base = {"a": {"x": 1}}
    current = {"a": 5}
    theirs = {"a": {"x": 2}}
    merged, conflicts = _merge(base, current, theirs)
    assert [c["path"] for c in conflicts] == ["/a"]


def test_both_create_different_value_conflicts():
    base = {}
    current = {"k": "server"}
    theirs = {"k": "client"}
    merged, conflicts = _merge(base, current, theirs)
    assert [c["path"] for c in conflicts] == ["/k"]


def test_both_create_same_nested_object_merges_subkeys():
    base = {}
    current = {"new": {"a": 1}}
    theirs = {"new": {"b": 2}}
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {"new": {"a": 1, "b": 2}}
    assert conflicts == []


def test_scalar_root_documents_merge():
    # 根不是对象时整体比较
    merged, conflicts = _merge({"_": 0}, None, None)  # 双方都改成 null
    assert merged is None and conflicts == []
    merged, conflicts = _merge(1, 2, 3)
    assert merged is MISSING and [c["path"] for c in conflicts] == [""]


def test_root_conflict_presented_with_empty_pointer():
    merged, conflicts = _merge([1], [2], [3])
    assert conflicts[0]["path"] == ""
    assert present_conflict(conflicts[0])["values"] == {"base": [1], "current": [2], "theirs": [3]}
    assert merged is MISSING


def test_json_pointer_escapes_tilde_and_slash():
    base = {"a/b": 1, "~x": 2}
    current = {"a/b": 10, "~x": 20}
    theirs = {"a/b": 100, "~x": 200}
    merged, conflicts = _merge(base, current, theirs)
    paths = {c["path"] for c in conflicts}
    assert paths == {"/a~1b", "/~0x"}


def test_noop_theirs_matching_current_generates_no_change():
    base = {"a": 1}
    current = {"a": 9}
    theirs = {"a": 9}   # 来稿基于旧基线的操作结果恰好等于当前版
    merged, conflicts = _merge(base, current, theirs)
    assert merged == {"a": 9} and conflicts == []


# ---------------- reverse_merge_patch（撤销补丁） ----------------


def _roundtrip(before, after):
    """生成逆向补丁并应用到 after，必须还原为 before。"""
    patch = reverse_merge_patch(before, after)
    assert apply_merge_patch(after, patch) == before
    return patch


def test_reverse_scalar_change():
    patch = _roundtrip({"a": 1, "b": 2}, {"a": 9, "b": 2})
    # 最小化：未变化的 b 不出现在补丁中
    assert patch == {"a": 1}


def test_reverse_added_key_is_deleted():
    patch = _roundtrip({"a": 1}, {"a": 1, "b": 2})
    assert patch == {"b": None}


def test_reverse_deleted_key_is_restored():
    patch = _roundtrip({"a": 1, "b": 2}, {"a": 1})
    assert patch == {"b": 2}


def test_reverse_real_null_value_distinguished_from_delete():
    # after 把 a 改成真实 null → 撤销改回旧值
    patch = _roundtrip({"a": 1}, {"a": None})
    assert patch == {"a": 1}
    # after 新增 a:null → 撤销应删除该键，而不是保留 null
    patch = _roundtrip({}, {"a": None})
    assert patch == {"a": None}
    assert apply_merge_patch({"a": None}, patch) == {}


def test_reverse_nested_changes_are_minimal():
    before = {"db": {"host": "h", "port": 5432}, "keep": 1}
    after = {"db": {"host": "h", "port": 6543}, "keep": 1}
    patch = _roundtrip(before, after)
    assert patch == {"db": {"port": 5432}}


def test_reverse_nested_add_and_delete():
    before = {"db": {"host": "h"}, "gone": 7}
    after = {"db": {"host": "h", "ssl": True}}
    patch = _roundtrip(before, after)
    assert patch == {"db": {"ssl": None}, "gone": 7}


def test_reverse_array_replaced_whole():
    before = {"items": [1, 2, 3]}
    after = {"items": [1, 2, 3, 4]}
    patch = _roundtrip(before, after)
    # 数组整体还原，不按下标回退
    assert patch == {"items": [1, 2, 3]}


def test_reverse_type_change_replaces_whole_value():
    before = {"a": {"x": 1}}
    after = {"a": 5}
    patch = _roundtrip(before, after)
    assert patch == {"a": {"x": 1}}


def test_reverse_non_object_root_replaces_entire_document():
    assert _roundtrip([1, 2], [9]) == [1, 2]
    assert _roundtrip({"a": 1}, None) == {"a": 1}
    assert _roundtrip(None, {"a": 1}) is None
    assert _roundtrip("old", "new") == "old"


def test_reverse_identical_documents_is_empty_patch():
    assert reverse_merge_patch({"a": 1, "b": [1]}, {"a": 1, "b": [1]}) == {}


def test_reverse_patch_does_not_mutate_inputs():
    before = {"a": {"x": 1}}
    after = {"a": {"x": 2}}
    reverse_merge_patch(before, after)
    assert before == {"a": {"x": 1}}
    assert after == {"a": {"x": 2}}
