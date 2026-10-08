import pytest

def test_cronistsource_import():
    try:
        from cronistsource.stage import run_stage
    except ImportError as e:
        pytest.fail(f"Failed to import run_stage: {e}")

    assert run_stage is not None, "run_stage should be imported successfully"
