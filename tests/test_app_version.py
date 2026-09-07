from logfather.core.app_version import is_newer, version_number


def test_version_number_and_is_newer():
    assert version_number("0.207") == 207
    assert version_number("v0.207") == 207
    assert version_number("dev") is None
    assert version_number("") is None
    assert is_newer("0.207", "0.206")
    assert not is_newer("0.206", "0.206")
    assert not is_newer("0.205", "0.206")
    assert not is_newer("dev", "0.206")
    assert not is_newer("0.207", None)
