import base64

import pytest

import welearn_common
from welearn_common import generate_cipher_text, parse_numeric_param


class TestGenerateCipherText:
    def test_roundtrip_with_fixed_now(self):
        E, T1 = generate_cipher_text("Passw0rd!", now_ms=1700000000000)
        decoded = base64.b64decode(E).decode('utf-8')
        t1_str, hex_part = decoded.split('*', 1)
        assert int(t1_str) == T1
        assert bytes.fromhex(hex_part) == "Passw0rd!".encode('utf-8')

    def test_t1_within_hundred_of_t0(self):
        T0 = 1700000000000
        _, T1 = generate_cipher_text("whatever", now_ms=T0)
        assert T0 // 100 * 100 <= T1 <= T0 // 100 * 100 + 99

    def test_deterministic_for_same_now(self):
        assert (generate_cipher_text("pw", now_ms=1234567890123)
                == generate_cipher_text("pw", now_ms=1234567890123))

    def test_non_ascii_password(self):
        pwd = "密码Abc123"
        E, T1 = generate_cipher_text(pwd, now_ms=999999999999)
        decoded = base64.b64decode(E).decode('utf-8')
        _, hex_part = decoded.split('*', 1)
        assert bytes.fromhex(hex_part) == pwd.encode('utf-8')


class TestParseNumericParam:
    def test_single_value(self):
        assert parse_numeric_param("30") == 30

    def test_single_value_with_spaces(self):
        assert parse_numeric_param("  100 ") == 100

    def test_range_value(self):
        assert parse_numeric_param("10,30") == (10, 30)

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_numeric_param("abc")

    def test_rejects_three_parts(self):
        with pytest.raises(ValueError):
            parse_numeric_param("10,30,40")

    def test_rejects_trailing_comma(self):
        with pytest.raises(ValueError):
            parse_numeric_param("10,")

    def test_rejects_inverted_range(self):
        with pytest.raises(ValueError):
            parse_numeric_param("30,10")


class TestFetchSections:
    def test_builds_url_and_parses_info(self):
        from tests.fakes import FakeResponse, FakeSession
        import welearn_common
        session = FakeSession()
        session.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
            FakeResponse(json_data={"info": [{"location": "L1", "iscomplete": "completed"}]})
        info = welearn_common.fetch_sections(session, "2833", "9", 2, "C1")
        assert info[0]["location"] == "L1"
        _method, url, _data = session.requests[0]
        assert url == ("https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"
                       "&cid=2833&uid=9&unitidx=2&classid=C1")

    def test_server_error_text_raises(self):
        from tests.fakes import FakeResponse, FakeSession
        import welearn_common
        session = FakeSession()
        session.routes["https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves"] = \
            FakeResponse(text="出错了")
        with pytest.raises(welearn_common.WelearnError):
            welearn_common.fetch_sections(session, "1", "1", 99, "C")


class TestNormalizeTargets:
    """normalize_targets 纯函数行为:多选单元/小节目标的解析与校验。"""

    def test_all_units_variants(self):
        assert welearn_common.normalize_targets(0) == (True, None, None)
        assert welearn_common.normalize_targets(None) == (True, None, None)

    def test_single_unit(self):
        assert welearn_common.normalize_targets(3) == (False, [3], None)

    def test_unit_list_dedup_sorted(self):
        assert welearn_common.normalize_targets([3, 1, 3]) == (False, [1, 3], None)

    def test_string_unit_elements_coerced(self):
        """JSON 键常为字符串;列表元素整体转 int(非逐字符)"""
        assert welearn_common.normalize_targets(["3", "1"]) == (False, [1, 3], None)

    def test_sections_map_normalized(self):
        assert welearn_common.normalize_targets([1, 3], {"3": [2, 5, 2]}) == \
            (False, [1, 3], {3: [2, 5]})

    def test_scalar_section_spreads_to_units(self):
        assert welearn_common.normalize_targets([1, 3], 2) == (False, [1, 3], {1: [2], 3: [2]})

    def test_empty_sections_entries_dropped(self):
        assert welearn_common.normalize_targets([1], {1: []}) == (False, [1], None)

    def test_empty_unit_list_rejected(self):
        with pytest.raises(ValueError, match="未选择任何单元"):
            welearn_common.normalize_targets([])

    def test_negative_unit_rejected(self):
        with pytest.raises(ValueError):
            welearn_common.normalize_targets(-1)
        with pytest.raises(ValueError):
            welearn_common.normalize_targets([0, 1])

    def test_negative_section_rejected(self):
        with pytest.raises(ValueError):
            welearn_common.normalize_targets([1], {1: [0]})

    def test_all_units_with_sections_rejected(self):
        with pytest.raises(ValueError, match="具体单元"):
            welearn_common.normalize_targets(0, {1: [1]})
        with pytest.raises(ValueError):
            welearn_common.normalize_targets(0, 3)

    def test_section_key_outside_unit_list_rejected(self):
        """小节键指向未选单元必须拒绝:静默丢弃会让整单元被刷(过度刷课)"""
        with pytest.raises(ValueError):
            welearn_common.normalize_targets([1], {5: [1]})

    def test_string_inputs_rejected(self):
        """裸字符串会逐字符迭代("12"→[1,2]),必须显式拒绝而非静默改目标"""
        with pytest.raises(ValueError):
            welearn_common.normalize_targets("12")
        with pytest.raises(ValueError):
            welearn_common.normalize_targets([1], "5")
        with pytest.raises(ValueError):
            welearn_common.normalize_targets([1], {1: "12"})
