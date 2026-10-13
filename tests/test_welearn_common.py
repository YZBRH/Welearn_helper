import base64

import pytest

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
