import pytest

from security_utils import is_plain_hostname


@pytest.mark.parametrize("d", ["acme.com", "a-b.example.co.uk", "x1.io"])
def test_plain_hostnames_accepted(d):
    assert is_plain_hostname(d)


@pytest.mark.parametrize("d", [
    "", "example", "ACME.com", "a\\b.com", "a.com\n", "a\n.com", "a.com\r\n", "a@b.com", "a.com:80", "a.com/x", "a b.com", "a%2e.com",
    "exämple.com", "-a.com", "a-.com", "a..com", "1.2.3.4", "[::1]", "a" * 64 + ".com",
    ("a" * 60 + ".") * 5 + "com",
])
def test_non_plain_hostnames_rejected(d):
    assert not is_plain_hostname(d)


def test_the_backslash_case_is_a_real_backslash_not_a_backspace():
    assert "a\\b.com"[1] == "\\" and "\b" not in "a\\b.com"
