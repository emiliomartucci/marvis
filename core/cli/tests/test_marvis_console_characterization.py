"""Characterization tests for the local Console launcher.

`marvis console` is how a user reaches the local GUI, and it had no coverage.
U7 of the surface-separation plan moves GUI ownership into a marvis-owned
directory, and its execution note requires the first diff to be
behavior-preserving — which is only checkable if the current behavior is
pinned first. These tests describe what the launcher does TODAY, so the move
either keeps it or fails loudly.

They intentionally assert observable contracts (the URL a user lands on, the
refusal paths, the unit files handed to the OS) rather than internals.
"""

from __future__ import annotations

import json

import pytest
import typer

from core.cli import marvis_console as mc


# --------------------------------------------------------------------------
# Where the user lands
# --------------------------------------------------------------------------


def test_launch_url_is_loopback_8100_ui():
    # The port and path are a user-visible contract and are referenced by the
    # autostart units and the health probe alike.
    assert mc._ui_url() == "http://127.0.0.1:8100/ui/"
    assert mc._base_url() == "http://127.0.0.1:8100"


def test_health_probe_order_is_healthz_then_health():
    assert mc._HEALTH_PATHS == ("/healthz", "/health")


# --------------------------------------------------------------------------
# Health parsing: only an explicit ok counts
# --------------------------------------------------------------------------


def test_health_accepts_only_status_ok():
    assert mc._parse_health(b'{"status":"ok"}') == {"status": "ok"}
    assert mc._parse_health(b'{"status":"ok","brain":"abc123"}')["brain"] == "abc123"


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"degraded"}',  # answering is not the same as healthy
        b'{"ok":true}',
        b"not json",
        b"[]",
        b"",
    ],
)
def test_health_rejects_anything_else(body):
    assert mc._parse_health(body) is None


# --------------------------------------------------------------------------
# Brain fingerprint: the guard against serving the wrong database
# --------------------------------------------------------------------------


def test_brain_fingerprint_is_stable_and_path_derived(tmp_path):
    db = tmp_path / "marvis.db"
    first = mc._brain_fingerprint_for(str(db))
    assert first == mc._brain_fingerprint_for(str(db))
    assert len(first) == 12
    assert first != mc._brain_fingerprint_for(str(tmp_path / "other.db"))


def test_brain_fingerprint_ignores_path_spelling(tmp_path):
    # Resolved before hashing, so the same database is never mistaken for two.
    db = tmp_path / "marvis.db"
    assert mc._brain_fingerprint_for(str(db)) == mc._brain_fingerprint_for(
        str(tmp_path / "." / "marvis.db")
    )


# --------------------------------------------------------------------------
# Packaged GUI detection
# --------------------------------------------------------------------------


def test_console_dist_missing_is_reported_not_raised(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("no package")

    monkeypatch.setattr("importlib.resources.files", explode)
    available, detail = mc._console_dist_available()
    assert available is False
    assert detail  # the reason is surfaced, not swallowed


def test_console_command_refuses_without_packaged_gui(monkeypatch):
    monkeypatch.setattr(mc, "_console_dist_available", lambda: (False, "missing"))
    started = []
    monkeypatch.setattr(mc, "_ensure_api_running", lambda: started.append(True) or (True, None))

    with pytest.raises(typer.Exit) as exc:
        mc.console_cmd(no_open=True, stop=False)

    assert exc.value.exit_code == 1
    # It must not start an API for a GUI that cannot be served.
    assert started == []


# --------------------------------------------------------------------------
# Reuse, refusal and the stale-brain restart (gh issue #16)
# --------------------------------------------------------------------------


def test_running_marvis_api_is_reused_without_restart(monkeypatch):
    monkeypatch.setattr(
        mc, "_probe_api", lambda **_: mc.ApiProbe(True, True, "/healthz", "", brain="same")
    )
    monkeypatch.setattr(mc, "_expected_brain_fingerprint", lambda: "same")
    monkeypatch.setattr(
        mc, "_start_api_process", lambda: pytest.fail("must not start a second API")
    )

    ok, log = mc._ensure_api_running()
    assert ok is True
    assert log is None


def test_foreign_process_on_the_port_is_refused(monkeypatch):
    # Something else owns 8100: reusing it would serve an unrelated app at /ui/.
    monkeypatch.setattr(
        mc, "_probe_api", lambda **_: mc.ApiProbe(True, False, None, "not marvis")
    )
    monkeypatch.setattr(
        mc, "_start_api_process", lambda: pytest.fail("must not start over a foreign process")
    )

    ok, _log = mc._ensure_api_running()
    assert ok is False


def test_api_serving_a_different_brain_is_restarted(monkeypatch):
    # An instance that survived a settings change serves the WRONG data; being
    # silently reused is the defect this branch exists to prevent.
    monkeypatch.setattr(
        mc, "_probe_api", lambda **_: mc.ApiProbe(True, True, "/healthz", "", brain="old")
    )
    monkeypatch.setattr(mc, "_expected_brain_fingerprint", lambda: "new")
    stopped = []
    monkeypatch.setattr(mc, "_stop_api_process", lambda **_: (stopped.append(True), (True, "ok"))[1])
    monkeypatch.setattr(mc, "_start_api_process", lambda: (object(), "/tmp/log"))
    monkeypatch.setattr(
        mc, "_wait_for_api", lambda _proc, **_kw: mc.ApiProbe(True, True, "/healthz", "")
    )

    ok, log = mc._ensure_api_running()
    assert stopped == [True]
    assert ok is True
    assert log == "/tmp/log"


def test_stale_brain_that_cannot_be_stopped_fails_closed(monkeypatch):
    monkeypatch.setattr(
        mc, "_probe_api", lambda **_: mc.ApiProbe(True, True, "/healthz", "", brain="old")
    )
    monkeypatch.setattr(mc, "_expected_brain_fingerprint", lambda: "new")
    monkeypatch.setattr(mc, "_stop_api_process", lambda **_: (False, "permission denied"))
    monkeypatch.setattr(
        mc, "_start_api_process", lambda: pytest.fail("must not start beside a stale API")
    )

    ok, _log = mc._ensure_api_running()
    assert ok is False


def test_unknown_expected_brain_does_not_force_a_restart(monkeypatch):
    # Postgres backends have no db_path and therefore no fingerprint; absence of
    # information must not be read as a mismatch.
    monkeypatch.setattr(
        mc, "_probe_api", lambda **_: mc.ApiProbe(True, True, "/healthz", "", brain="something")
    )
    monkeypatch.setattr(mc, "_expected_brain_fingerprint", lambda: None)
    monkeypatch.setattr(mc, "_start_api_process", lambda: pytest.fail("must not restart"))

    ok, _log = mc._ensure_api_running()
    assert ok is True


# --------------------------------------------------------------------------
# What we hand to the operating system
# --------------------------------------------------------------------------


def test_systemd_unit_restarts_on_failure_with_a_rate_limit():
    text = mc.systemd_service_text("/usr/bin/marvis", ["serve"])
    assert "ExecStart=/usr/bin/marvis serve" in text
    assert "Restart=on-failure" in text
    # Without a burst limit a crash-looping server would restart forever.
    assert "StartLimitBurst=3" in text
    assert "WantedBy=default.target" in text


def test_systemd_unit_quotes_arguments_with_spaces():
    text = mc.systemd_service_text("/opt/my apps/marvis", ["serve"])
    assert "ExecStart='/opt/my apps/marvis' serve" in text


def test_launchd_plist_is_valid_and_points_at_the_command():
    import plistlib

    text = mc.launchd_plist_text("/usr/local/bin/marvis", ["serve"])
    parsed = plistlib.loads(text.encode("utf-8"))
    assert parsed["ProgramArguments"] == ["/usr/local/bin/marvis", "serve"]
    assert parsed["Label"] == mc._LABEL


def test_windows_registration_script_mentions_the_task_name():
    script = mc.windows_register_task_script("C:\\marvis.exe", ["serve"])
    assert mc._WINDOWS_TASK in script


# --------------------------------------------------------------------------
# Settings resolution
# --------------------------------------------------------------------------


def test_settings_path_prefers_explicit_override(monkeypatch, tmp_path):
    explicit = tmp_path / "custom.yaml"
    monkeypatch.setenv("MARVIS_SETTINGS_PATH", str(explicit))
    monkeypatch.setenv("MARVIS_VAULT_DIR", str(tmp_path / "vault"))
    assert mc._settings_yaml_path() == explicit


def test_settings_path_falls_back_to_vault_then_home(monkeypatch, tmp_path):
    monkeypatch.delenv("MARVIS_SETTINGS_PATH", raising=False)
    monkeypatch.setenv("MARVIS_VAULT_DIR", str(tmp_path / "vault"))
    assert mc._settings_yaml_path() == tmp_path / "vault" / "settings.yaml"

    monkeypatch.delenv("MARVIS_VAULT_DIR", raising=False)
    assert mc._settings_yaml_path().name == "settings.yaml"
    assert mc._settings_yaml_path().parent.name == ".marvis"


def test_settings_env_is_empty_when_no_settings_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MARVIS_SETTINGS_PATH", str(tmp_path / "absent.yaml"))
    assert mc._settings_env() == {}


def test_health_payload_without_brain_yields_no_fingerprint():
    payload = mc._parse_health(json.dumps({"status": "ok"}).encode())
    assert payload is not None
    assert payload.get("brain") is None
