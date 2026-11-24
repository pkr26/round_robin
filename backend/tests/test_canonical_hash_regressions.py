from copy import deepcopy

from round_robin.database.services import canonical_hash


def test_persisted_json_fingerprint_keeps_utf8_compact_sorted_encoding():
    value = {"nested": {"z": 1, "a": 2}, "b": ["β", 1, True, None], "a": "é雪"}
    original = deepcopy(value)
    # SHA-256 of the UTF-8 bytes of the historical compact serialized object:
    # {"a":"é雪","b":["β",1,true,null],"nested":{"a":2,"z":1}}
    assert canonical_hash(value) == "e9353d0ab9351c06e578d57bce5fe553525715039bcc19ce12094d1e753c9002"
    assert value == original


def test_map_order_does_not_change_fingerprint_but_values_do():
    assert canonical_hash({"b": 2, "a": 1}) == "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    assert canonical_hash({"a": 1, "b": 2}) == "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    assert canonical_hash({"a": 1, "b": 3}) == "f9c6777fb86597920de313c707c2c0aa7b059e208a66e1f56f7a2b548e11453d"


def test_array_order_and_json_types_remain_part_of_fingerprint():
    assert canonical_hash({"items": ["first", "second"]}) != canonical_hash({"items": ["second", "first"]})
    assert canonical_hash({"value": 1}) != canonical_hash({"value": "1"})
    assert canonical_hash({"value": False}) != canonical_hash({"value": None})
