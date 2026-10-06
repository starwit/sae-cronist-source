import pytest

def test_mystage_import():
    try:
        from mystage.mystage import MyStage
    except ImportError as e:
        pytest.fail(f"Failed to import MyStage: {e}")

    assert MyStage is not None, "MyStage should be imported successfully"