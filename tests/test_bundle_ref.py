"""
tests/test_bundle_ref.py

Tests for core/bundle_ref — GB bundle path storage and resolution.

Coverage:
  - store_bundle_ref: path string form, ~ compression, alias presence
  - resolve_gb_bundle: alias-first, path fallback, both-fail error
  - make_alias / resolve_alias: skipped on non-macOS; round-trip on macOS
  - Edge cases: missing file, None inputs, non-home paths
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bundle_ref import (
    make_alias,
    resolve_alias,
    resolve_gb_bundle,
    store_bundle_ref,
)

IS_MACOS = sys.platform == "darwin"


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def make_band_bundle(tmp: Path, name: str = "Test") -> Path:
    """Create a minimal .band bundle so the path exists on disk."""
    band = tmp / f"{name}.band"
    (band / "Alternatives" / "000").mkdir(parents=True)
    (band / "Alternatives" / "000" / "ProjectData").write_bytes(b"\x00" * 64)
    return band


# ─────────────────────────────────────────────────────────────
# store_bundle_ref
# ─────────────────────────────────────────────────────────────

class TestStoreBundleRef:
    def test_returns_string_path(self, tmp_path):
        band = make_band_bundle(tmp_path)
        path_str, _ = store_bundle_ref(band)
        assert isinstance(path_str, str)
        assert path_str.endswith(".band")

    def test_compresses_home_to_tilde(self, tmp_path, monkeypatch):
        """Paths under home dir should be stored as ~/..."""
        fake_home = tmp_path / "home" / "user"
        fake_home.mkdir(parents=True)
        band = fake_home / "Music" / "Test.band"
        (band / "Alternatives" / "000").mkdir(parents=True)
        (band / "Alternatives" / "000" / "ProjectData").write_bytes(b"\x00" * 64)

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        path_str, _ = store_bundle_ref(band)
        assert path_str.startswith("~/")

    def test_non_home_path_stored_absolute(self, tmp_path):
        """Paths outside home should be stored as absolute."""
        # tmp_path is typically /tmp/... which is not under home
        band = make_band_bundle(tmp_path)
        # Patch home to something that definitely doesn't contain tmp_path
        with patch("core.bundle_ref.Path.home", return_value=Path("/nonexistent/home")):
            path_str, _ = store_bundle_ref(band)
        assert path_str.startswith("/") or ":" in path_str  # absolute

    def test_alias_is_none_on_non_macos(self, tmp_path):
        if IS_MACOS:
            pytest.skip("Non-macOS specific test")
        band = make_band_bundle(tmp_path)
        _, alias = store_bundle_ref(band)
        assert alias is None

    def test_alias_is_string_or_none(self, tmp_path):
        """alias is always str or None, never other types."""
        band = make_band_bundle(tmp_path)
        _, alias = store_bundle_ref(band)
        assert alias is None or isinstance(alias, str)

    def test_alias_is_valid_base64_when_present(self, tmp_path):
        band = make_band_bundle(tmp_path)
        _, alias = store_bundle_ref(band)
        if alias is not None:
            # Should not raise
            decoded = base64.b64decode(alias)
            assert len(decoded) > 0


# ─────────────────────────────────────────────────────────────
# make_alias / resolve_alias
# ─────────────────────────────────────────────────────────────

class TestMakeAlias:
    def test_returns_none_on_non_macos(self, tmp_path):
        if IS_MACOS:
            pytest.skip("Non-macOS specific test")
        band = make_band_bundle(tmp_path)
        assert make_alias(band) is None

    @pytest.mark.skipif(not IS_MACOS, reason="macOS only")
    def test_returns_string_on_macos_when_pyobjc_available(self, tmp_path):
        """On macOS with PyObjC this should return a base64 string."""
        band = make_band_bundle(tmp_path)
        result = make_alias(band)
        # May be None if PyObjC unavailable in test environment
        if result is not None:
            assert isinstance(result, str)
            base64.b64decode(result)  # must be valid base64

    def test_returns_none_when_pyobjc_missing(self, tmp_path):
        """If Foundation import fails, make_alias returns None gracefully."""
        band = make_band_bundle(tmp_path)
        with patch.dict("sys.modules", {"Foundation": None}):
            # Force re-evaluation of _IS_MACOS to True so we enter macOS branch
            with patch("core.bundle_ref._IS_MACOS", True):
                result = make_alias(band)
        # Should not raise — returns None on import failure
        assert result is None


class TestResolveAlias:
    def test_returns_none_on_non_macos(self):
        if IS_MACOS:
            pytest.skip("Non-macOS specific test")
        result = resolve_alias("dGVzdA==")  # valid base64 for "test"
        assert result is None

    def test_returns_none_on_garbage_data(self):
        """Garbage base64 should not raise — returns None."""
        with patch("core.bundle_ref._IS_MACOS", True):
            with patch.dict("sys.modules", {"Foundation": None}):
                result = resolve_alias("dGVzdA==")
        assert result is None

    @pytest.mark.skipif(not IS_MACOS, reason="macOS only")
    def test_round_trip_on_macos(self, tmp_path):
        """make_alias → resolve_alias should return the original path."""
        band = make_band_bundle(tmp_path)
        alias = make_alias(band)
        if alias is None:
            pytest.skip("PyObjC not available in this environment")
        resolved = resolve_alias(alias)
        assert resolved is not None
        assert resolved.resolve() == band.resolve()


# ─────────────────────────────────────────────────────────────
# resolve_gb_bundle
# ─────────────────────────────────────────────────────────────

class TestResolveGbBundle:
    def test_resolves_from_stored_path(self, tmp_path):
        band = make_band_bundle(tmp_path)
        path, err = resolve_gb_bundle(str(band), None)
        assert err == ""
        assert path is not None
        assert path.exists()

    def test_returns_error_when_stored_path_missing(self, tmp_path):
        missing = str(tmp_path / "DoesNotExist.band")
        path, err = resolve_gb_bundle(missing, None)
        assert path is None
        assert "not found" in err.lower() or "set-gb" in err

    def test_returns_error_when_nothing_stored(self):
        path, err = resolve_gb_bundle(None, None)
        assert path is None
        assert "set-gb" in err or "no garageband" in err.lower()

    def test_prefers_alias_over_path(self, tmp_path):
        """When alias resolves, it should be returned even if stored path differs."""
        band = make_band_bundle(tmp_path)
        # Mock resolve_alias to return our band path
        with patch("core.bundle_ref.resolve_alias", return_value=band):
            path, err = resolve_gb_bundle("/some/other/path.band", "fakealias==")
        assert err == ""
        assert path == band

    def test_falls_back_to_path_when_alias_stale(self, tmp_path):
        """Stale alias (returns None) should fall back to stored path."""
        band = make_band_bundle(tmp_path)
        with patch("core.bundle_ref.resolve_alias", return_value=None):
            path, err = resolve_gb_bundle(str(band), "stalealias==")
        assert err == ""
        assert path is not None
        assert path.exists()

    def test_both_fail_returns_error(self, tmp_path):
        """Stale alias + missing path = clear error message."""
        with patch("core.bundle_ref.resolve_alias", return_value=None):
            path, err = resolve_gb_bundle(
                str(tmp_path / "missing.band"),
                "stalealias==",
            )
        assert path is None
        assert len(err) > 0

    def test_alias_with_none_path_succeeds(self, tmp_path):
        """Alias-only resolution (no stored path string) should work."""
        band = make_band_bundle(tmp_path)
        with patch("core.bundle_ref.resolve_alias", return_value=band):
            path, err = resolve_gb_bundle(None, "somealias==")
        assert err == ""
        assert path == band

    def test_path_expanduser(self, tmp_path):
        """Stored paths with ~ should be expanded correctly.

        Note: Path.expanduser() is C-level on CPython and reads HOME from
        the environment, so we patch os.environ rather than Path.home().
        """
        import os
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        band = fake_home / "Music" / "Test.band"
        (band / "Alternatives" / "000").mkdir(parents=True)
        (band / "Alternatives" / "000" / "ProjectData").write_bytes(b"\x00" * 64)

        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(fake_home)
            path, err = resolve_gb_bundle("~/Music/Test.band", None)
            assert err == ""
            assert path is not None
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home


# ─────────────────────────────────────────────────────────────
# Integration: store_bundle_ref → resolve_gb_bundle round-trip
# ─────────────────────────────────────────────────────────────

class TestStoreThenResolve:
    def test_store_then_resolve_path(self, tmp_path):
        band = make_band_bundle(tmp_path)
        path_str, alias = store_bundle_ref(band)
        resolved, err = resolve_gb_bundle(path_str, alias)
        assert err == ""
        assert resolved is not None
        assert resolved.exists()

    def test_store_then_resolve_alias_preferred(self, tmp_path):
        """When alias resolves, it wins even if stored path string differs."""
        band = make_band_bundle(tmp_path)
        path_str, alias = store_bundle_ref(band)

        # Corrupt the stored path — alias should still work on macOS
        if IS_MACOS and alias is not None:
            resolved, err = resolve_gb_bundle("/wrong/path.band", alias)
            assert err == ""
            assert resolved is not None
        else:
            # On non-macOS, falls back to path_str
            resolved, err = resolve_gb_bundle(path_str, alias)
            assert err == ""
            assert resolved is not None
