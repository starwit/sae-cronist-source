import pytest

def test_cronistsource_import():
    try:
        from cronistsource.cronistsource import CronistSource
    except ImportError as e:
        pytest.fail(f"Failed to import CronistSource: {e}")

    assert CronistSource is not None, "CronistSource should be imported successfully"