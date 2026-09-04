"""CP009 — centralized path_policy tests."""

from groundtruth.delivery.path_policy import is_delivery_excluded, is_generated, is_vendored_path


def test_vendor_paths_excluded():
    assert is_vendored_path("vendor/jquery/foo.min.js")
    assert is_vendored_path("node_modules/pkg/index.js")
    assert not is_vendored_path("src/distribute.py")


def test_generated_markers():
    assert is_generated("api/run_function.pb.go")
    assert not is_generated("src/main.py")


def test_tailwind_asset_excluded():
    assert is_delivery_excluded("static/tailwind.min.js")
    assert is_delivery_excluded("assets/tailwind.config.js")
