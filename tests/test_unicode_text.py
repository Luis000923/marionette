"""Spanish (and any Unicode) text must reach the target app byte-exact.

How that is achieved differs by backend, and the tests say which one they hold
for:

* **Wayland** -- `ydotool type` sends US-layout keycodes for ASCII and drops
  everything else, so on a Spanish layout "presentación a:b/c" arrived as
  "presentacin aÑb-c". Anything with accents, ñ or symbols is pasted through the
  clipboard instead, and the CLI keeps UTF-8 intact from argv/stdin to wl-copy
  whatever the locale says;
* **Windows** -- key events carry the characters themselves, so the same text is
  *typed* and comes out exactly, clipboard untouched (only characters outside the
  Basic Multilingual Plane, i.e. emoji, have to be pasted). argv and stdout are
  UTF-16 there, so the locale repairs below do not apply.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from control import cli
from control import input as inp

from .test_browser_ai import _main

SPANISH = "¿Qué tal? Año nuevo: señor Núñez, pingüino, acción ¡ya! «comillas» — 5 €"
ACCENTS = "áéíóúÁÉÍÓÚñÑüÜ¿¡"
#: Typed exactly on Windows, pasted on Wayland.
TYPED_ON_WINDOWS = ["Hola mundo", "Linea 1\nLinea 2", "abc, 123.", "", SPANISH, ACCENTS,
                    "a:b/c", "user@mail.com", "(x)", "€", "日本"]
#: Pasted on both: ydotool cannot type them, and a key event cannot carry them.
PASTED_ON_BOTH = ["🙂", "\U0001F600\U0001F44B"]


def _completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- choosing typing vs pasting ------------------------------------------------------

class TestChooseMethod:
    @pytest.mark.parametrize("text", TYPED_ON_WINDOWS)
    def test_text_a_key_event_can_carry_is_typed(self, text):
        # On Wayland only the layout-safe subset of this list is typed, so the
        # assertion is narrowed there; see TestChooseMethodWayland below.
        if os.name != "nt" and not all(ch in inp._LAYOUT_SAFE for ch in text):
            pytest.skip("ydotool would drop or remap these characters")
        with patch("control.input.clipboard_available", return_value=True):
            assert inp.choose_text_method(text) == ("type", None)

    @pytest.mark.parametrize("text", PASTED_ON_BOTH)
    def test_characters_no_key_event_can_carry_are_pasted(self, text):
        with patch("control.input.clipboard_available", return_value=True):
            assert inp.choose_text_method(text) == ("paste", None)

    @pytest.mark.skipif(os.name != "posix", reason="ydotool drops non-ASCII")
    @pytest.mark.parametrize("text", ["presentación", "aquí", "niño", "¿qué?", "pingüino",
                                      "a:b/c", "user@mail.com", "(x)", "€", "日本"])
    def test_anything_ydotool_would_drop_or_remap_is_pasted(self, text):
        with patch("control.input.clipboard_available", return_value=True):
            assert inp.choose_text_method(text) == ("paste", None)

    @pytest.mark.skipif(os.name == "posix", reason="Windows has no missing clipboard")
    @pytest.mark.parametrize("text", PASTED_ON_BOTH)
    def test_the_clipboard_is_always_available_on_windows(self, text):
        assert inp.clipboard_available() is True
        assert inp.choose_text_method(text) == ("paste", None)

    @pytest.mark.skipif(os.name != "posix", reason="wl-clipboard missing")
    def test_accents_without_wl_clipboard_fail_instead_of_losing_characters(self):
        with patch("control.input.clipboard_available", return_value=False):
            with pytest.raises(inp.ClipboardError, match="wl-clipboard") as exc:
                inp.choose_text_method("presentación niño")
        assert "ó" in str(exc.value) and "ñ" in str(exc.value)

    @pytest.mark.skipif(os.name != "posix", reason="wl-clipboard missing")
    def test_ascii_symbols_without_wl_clipboard_are_typed_with_a_warning(self):
        with patch("control.input.clipboard_available", return_value=False):
            method, warning = inp.choose_text_method("a:b/c")
        assert method == "type" and "US keyboard layout" in warning

    @pytest.mark.skipif(os.name != "posix", reason="ydotool drops non-ASCII")
    def test_forcing_type_warns_which_characters_will_be_dropped(self):
        method, warning = inp.choose_text_method("año", "type")
        assert method == "type" and "ñ" in warning

    @pytest.mark.skipif(os.name == "posix", reason="Windows has no missing clipboard")
    def test_forcing_type_warns_about_characters_it_cannot_carry(self):
        method, warning = inp.choose_text_method("hola \U0001F600", "type")
        assert method == "type" and "\U0001F600" in warning
        assert inp.choose_text_method("hola señor", "type") == ("type", None)

    @pytest.mark.skipif(os.name != "posix", reason="wl-clipboard missing")
    def test_forcing_paste_needs_wl_clipboard(self):
        with patch("control.input.clipboard_available", return_value=False):
            with pytest.raises(inp.ClipboardError):
                inp.choose_text_method("hola", "paste")


# --- the clipboard keeps UTF-8 byte-exact ---------------------------------------------

class TestClipboardBytes:
    @pytest.mark.skipif(os.name == "nt", reason="wl-copy backend")
    def test_copy_sends_utf8_bytes(self):
        with patch("control.input.shutil.which", return_value="/usr/bin/wl-copy"), \
             patch("control.input.subprocess.run", return_value=_completed()) as run:
            inp.copy_to_clipboard(SPANISH)
        assert run.call_args[1]["input"] == SPANISH.encode("utf-8")
        assert run.call_args[1]["input"].decode("utf-8") == SPANISH

    @pytest.mark.skipif(os.name == "nt", reason="wl-paste backend")
    def test_read_decodes_utf8_whatever_the_locale(self, monkeypatch):
        monkeypatch.setenv("LANG", "es_ES.ISO-8859-1")
        with patch("control.input.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch("control.input.subprocess.run",
                   return_value=_completed(stdout=ACCENTS.encode("utf-8"))) as run:
            assert inp.read_clipboard() == ACCENTS
        assert "text" not in run.call_args[1]  # bytes, not the locale's decoding

    @pytest.mark.skipif(os.name == "nt", reason="wl-copy backend")
    def test_mojibake_never_appears_in_a_round_trip(self):
        stored = {}

        def fake_run(args, **kwargs):
            if args[0].endswith("wl-copy"):
                stored["data"] = kwargs["input"]
                return _completed()
            return _completed(stdout=stored["data"])

        with patch("control.input.shutil.which", side_effect=lambda t: f"/usr/bin/{t}"), \
             patch("control.input.subprocess.run", side_effect=fake_run):
            inp.copy_to_clipboard("presentación aquí")
            text = inp.read_clipboard()
        assert text == "presentación aquí"
        assert "Ã" not in text


# --- pasting ------------------------------------------------------------------------------

class TestPasteText:
    def _run(self, text, saved, keys=inp.DEFAULT_PASTE_KEYS):
        events = []
        with patch("control.input._save_clipboard", side_effect=lambda: events.append("save") or saved), \
             patch("control.input.copy_to_clipboard",
                   side_effect=lambda data, mime=None: events.append(("copy", data, mime))), \
             patch("control.input.key_combo", side_effect=lambda *k: events.append(("keys", k))), \
             patch("control.input.clear_clipboard", side_effect=lambda: events.append("clear")), \
             patch("control.input._sleep", side_effect=lambda s: events.append(("sleep", s))):
            restored = inp.paste_text(text, keys=keys)
        return restored, events

    def test_pastes_the_exact_text_then_restores_the_previous_clipboard(self):
        restored, events = self._run(SPANISH, ("text/plain", "lo que había".encode()))
        assert events == [
            "save",
            ("copy", SPANISH, None),
            ("keys", ("ctrl", "v")),
            ("sleep", inp.PASTE_SETTLE),
            ("copy", "lo que había".encode(), "text/plain"),
        ]
        assert restored is True

    def test_an_empty_clipboard_is_left_empty(self):
        restored, events = self._run("año", ("", b""))
        assert events[-1] == "clear" and restored is True

    def test_contents_that_cannot_be_restored_are_reported(self):
        restored, events = self._run("año", None)  # e.g. an image was copied
        assert restored is False
        assert [e for e in events if isinstance(e, tuple) and e[0] == "copy"] == [("copy", "año", None)]

    def test_the_whole_paste_is_one_input_gesture(self):
        """Copy and Ctrl+V happen under one hold: no other agent can cut in between."""
        held = []
        with patch("control.input._save_clipboard", return_value=None), \
             patch("control.input.copy_to_clipboard",
                   side_effect=lambda *a, **k: held.append(inp.input_mutex.held)), \
             patch("control.input.key_combo",
                   side_effect=lambda *k: held.append(inp.input_mutex.held)), \
             patch("control.input._sleep"):
            inp.paste_text("ñ")
        assert held == [True, True]
        assert not inp.input_mutex.held  # released afterwards

    @pytest.mark.parametrize("cls,keys", [("kitty", ("ctrl", "shift", "v")),
                                          ("org.kde.konsole", ("ctrl", "shift", "v")),
                                          ("CASCADIA_HOSTING_WINDOW_CLASS WindowsTerminal.exe",
                                           ("ctrl", "shift", "v")),
                                          ("brave-browser chrome", ("ctrl", "v")),
                                          (None, ("ctrl", "v"))])
    def test_terminals_paste_with_ctrl_shift_v(self, cls, keys):
        assert inp.paste_keys_for(cls) == keys


# --- the CLI: argv, stdin and stdout stay UTF-8 ----------------------------------------------

class TestTypeCommand:
    def _patch(self, monkeypatch, available=True):
        calls = []
        monkeypatch.setattr(cli.inp, "clipboard_available", lambda: available)
        monkeypatch.setattr(cli.inp, "paste_text",
                            lambda text, keys: calls.append(("paste", text, keys)) or True)
        monkeypatch.setattr(cli.inp, "type_text",
                            lambda text, delay_ms=0: calls.append(("type", text)))
        return calls

    def test_spanish_text_reaches_the_app_byte_exact(self, monkeypatch, capsys):
        """Whatever the backend, the text goes in whole and only its length is echoed."""
        calls = self._patch(monkeypatch)
        assert _main(["--json", "type", SPANISH], monkeypatch) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["chars"] == len(SPANISH)
        if os.name == "nt":
            # A key event carries ñ, ¿, « » and € exactly: typed, clipboard untouched.
            assert calls == [("type", SPANISH)] and data["method"] == "type"
        else:
            assert calls == [("paste", SPANISH, ("ctrl", "v"))]
            assert data["method"] == "paste" and data["clipboard_restored"] is True

    def test_plain_text_is_still_typed(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        _main(["--json", "type", "Hola mundo"], monkeypatch)
        assert calls == [("type", "Hola mundo")]

    @pytest.mark.skipif(os.name == "posix", reason="Windows always has a clipboard")
    def test_emoji_are_pasted_because_no_key_event_can_carry_them(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        _main(["--json", "type", "hola \U0001F600"], monkeypatch)
        assert calls == [("paste", "hola \U0001F600", ("ctrl", "v"))]
        assert json.loads(capsys.readouterr().out)["method"] == "paste"

    @pytest.mark.skipif(os.name == "nt", reason="Windows has no missing clipboard")
    def test_no_wl_clipboard_is_a_clear_error_and_nothing_is_typed(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch, available=False)
        assert _main(["--json", "type", "presentación"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "clipboard_error" and "wl-clipboard" in err["hint"]
        assert calls == []

    @pytest.mark.skipif(os.name == "nt", reason="Windows drops nothing when typing")
    def test_method_type_is_honoured_with_a_warning(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        _main(["--json", "type", "año", "--method", "type"], monkeypatch)
        out = capsys.readouterr()
        assert calls == [("type", "año")]
        assert "ñ" in json.loads(out.out)["warning"] and "[WARN]" in out.err

    @pytest.mark.skipif(os.name == "nt", reason="pasting is not the default there")
    def test_paste_keys_follow_the_window_or_the_override(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        monkeypatch.setattr(cli.windows, "active_window",
                            lambda: cli.windows.Window("{1}", "zsh", "kitty"))
        _main(["--json", "type", "cd ~/Música"], monkeypatch)
        _main(["--json", "type", "cd ~/Música", "--paste-keys", "shift+insert"], monkeypatch)
        assert [c[2] for c in calls] == [("ctrl", "shift", "v"), ("shift", "insert")]

    def test_stdin_is_read_as_utf8_even_under_a_latin1_locale(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        stdin = io.TextIOWrapper(io.BytesIO(SPANISH.encode("utf-8")), encoding="latin-1")
        monkeypatch.setattr("sys.stdin", stdin)
        _main(["--json", "type", "-"], monkeypatch)
        assert calls[0][1] == SPANISH

    def test_a_utf8_bom_on_stdin_is_dropped(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"\xef\xbb\xbfa\xc3\xb1o")))
        _main(["--json", "type", "-"], monkeypatch)
        assert calls[0][1] == "año"

    def test_invalid_utf8_on_stdin_is_rejected_not_mangled(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO("año".encode("latin-1"))))
        assert _main(["--json", "type", "-"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "encoding_error" and "iconv" in err["hint"]
        assert calls == []

    def test_clip_set_reads_stdin_as_utf8_too(self, monkeypatch, capsys):
        copied = []
        monkeypatch.setattr(cli.inp, "copy_to_clipboard", lambda text, mime=None: copied.append(text))
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(ACCENTS.encode()), encoding="ascii"))
        _main(["--json", "clip", "set", "-"], monkeypatch)
        assert copied == [ACCENTS]


class TestArgvAndStdout:
    def test_utf8_args_pass_through_untouched(self):
        assert cli.utf8_arg(SPANISH) == SPANISH

    def test_latin1_decoded_argv_is_repaired(self, monkeypatch):
        # What Python hands us under a Latin-1 locale for the UTF-8 bytes of "presentación".
        mangled = "presentación".encode("utf-8").decode("latin-1")
        assert mangled == "presentaciÃ³n"
        monkeypatch.setattr(sys, "getfilesystemencoding", lambda: "iso8859-1")
        assert cli.utf8_arg(mangled) == "presentación"

    def test_real_latin1_text_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(sys, "getfilesystemencoding", lambda: "iso8859-1")
        assert cli.utf8_arg("año") == "año"  # b"a\xf1o" is not UTF-8: it really was Latin-1

    @pytest.mark.skipif(os.name == "nt", reason="Windows argv is UTF-16, never Latin-1")
    def test_surrogate_escaped_bytes_are_decoded_as_utf8(self, monkeypatch):
        monkeypatch.setattr(sys, "getfilesystemencoding", lambda: "ascii")
        escaped = "niño".encode("utf-8").decode("ascii", "surrogateescape")
        assert cli.utf8_arg(escaped) == "niño"

    @pytest.mark.skipif(os.name == "nt", reason="Windows argv is UTF-16, never Latin-1")
    def test_main_repairs_argv_before_parsing(self, monkeypatch, capsys):
        typed = []
        monkeypatch.setattr(cli.inp, "clipboard_available", lambda: True)
        monkeypatch.setattr(cli.inp, "paste_text", lambda text, keys: typed.append(text) or True)
        monkeypatch.setattr(sys, "getfilesystemencoding", lambda: "iso8859-1")
        _main(["--json", "type", "acción".encode().decode("latin-1")], monkeypatch)
        assert typed == ["acción"]

    def test_a_non_utf8_stdout_is_switched_to_utf8(self, monkeypatch):
        raw = io.BytesIO()
        monkeypatch.setattr("sys.stdout", io.TextIOWrapper(raw, encoding="ascii"))
        cli._utf8_stdio()
        print("ñandú", end="")
        sys.stdout.flush()
        assert raw.getvalue() == "ñandú".encode("utf-8")

    def test_clip_get_prints_accents_intact(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.inp, "read_clipboard", lambda mime=None: SPANISH)
        _main(["clip", "get"], monkeypatch)
        assert capsys.readouterr().out == SPANISH + "\n"


@pytest.mark.skipif(os.name == "nt", reason="ydotool backend")
def test_ydotool_gets_utf8_arguments_under_an_ascii_locale(monkeypatch):
    monkeypatch.setattr(sys, "getfilesystemencoding", lambda: "ascii")
    with patch("control.input.subprocess.run", return_value=_completed(stdout="", stderr="")) as run:
        inp.type_text("señor")
    assert run.call_args[0][0] == [b"ydotool", b"type", "señor".encode("utf-8")]
