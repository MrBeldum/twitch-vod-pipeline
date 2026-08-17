"""Static checks on the dashboard page itself.

These exist because the API was verified with curl and the page was never opened
in a browser. That gap hid a total blocker: `.modal` and `.drawer` both set
`display`, author styles beat the user agent's `[hidden] { display: none }`
whatever their specificity, so the `hidden` attribute did nothing. Both overlays
were visible from page load and could never be dismissed -- Close, Escape and
backdrop-click all set `hidden` -- while a `position: fixed` overlay swallowed
every click on the page beneath.

Nothing here renders a page. They are cheap structural checks against the class
of mistake that a JSON-level test cannot see.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "vodpipe" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

# Comments are stripped before anything is matched. The rule these tests check is
# explained in a comment that quotes the broken CSS verbatim, and matching that
# quotation instead of the real declaration is exactly the false pass to avoid.
CSS = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(encoding="utf-8"),
             flags=re.S)


class HiddenAttributeTests(unittest.TestCase):
    def test_hidden_is_forced_to_win(self):
        """Without this rule, any class that sets `display` defeats `hidden`."""
        match = re.search(r"\[hidden\]\s*\{([^}]*)\}", CSS)
        self.assertIsNotNone(match, "style.css needs a [hidden] rule")
        body = match.group(1).replace(" ", "").lower()
        self.assertIn("display:none", body)
        self.assertIn("!important", body,
                      "a plain `display: none` loses to a later author rule")

    def test_every_element_toggled_by_hidden_starts_hidden_in_the_markup(self):
        for element_id in self.toggled_ids():
            pattern = rf'id="{re.escape(element_id)}"[^>]*>'
            tag = re.search(pattern, HTML)
            self.assertIsNotNone(tag, f"#{element_id} is not in index.html")
            self.assertIn("hidden", tag.group(0),
                          f"#{element_id} is toggled by `hidden` but does not "
                          "carry it initially, so it shows on page load")

    def test_overlays_that_set_display_are_all_covered(self):
        """The specific trap: a fixed overlay with its own `display`."""
        overlays = [name for name in ("modal", "drawer")
                    if re.search(rf"\.{name}\s*\{{[^}}]*display\s*:", CSS)]
        self.assertTrue(overlays, "expected .modal/.drawer to set display")
        # Covered by the [hidden] override above; this asserts they still are.
        self.assertRegex(CSS, r"\[hidden\][^{]*\{[^}]*display\s*:\s*none\s*!important")

    def toggled_ids(self) -> set[str]:
        """Element ids that app.js shows and hides via the `hidden` property."""
        return set(re.findall(r"\$\('#([\w-]+)'\)\.hidden\s*=", JS))


class SelectorIntegrityTests(unittest.TestCase):
    """Every id the script reaches for has to exist, or a control silently dies."""

    def script_ids(self) -> set[str]:
        found = set(re.findall(r"\$\('#([\w-]+)'\)", JS))
        found |= set(re.findall(r"getElementById\('([\w-]+)'\)", JS))
        return found

    def markup_ids(self) -> set[str]:
        return set(re.findall(r'id="([\w-]+)"', HTML))

    def test_no_script_selector_is_dangling(self):
        missing = sorted(self.script_ids() - self.markup_ids())
        self.assertEqual(missing, [],
                         f"app.js addresses ids that index.html does not define: "
                         f"{missing}")

    def test_the_panels_render_targets_exist(self):
        for element_id in ("channels", "live", "sessions", "jobs", "capabilities",
                           "disk", "connection", "toast", "settings-fields"):
            self.assertIn(element_id, self.markup_ids(), element_id)


class ProductReadinessTests(unittest.TestCase):
    def test_file_preview_uses_opaque_artifact_ids(self):
        self.assertIn("/api/file?artifact_id=", JS)
        self.assertIn("file.artifact_id", JS)
        self.assertNotIn("/api/file?path=", JS)

    def test_poll_failures_have_a_visible_stale_state(self):
        self.assertIn('id="connection"', HTML)
        self.assertIn("Disconnected · last successful refresh:", JS)
        self.assertIn("state.refreshError", JS)
        self.assertRegex(CSS, r"\.connection\.stale\s*\{")

    def test_session_errors_are_rendered_outside_the_expanded_body(self):
        error = JS.index("if (session.error)")
        expanded = JS.index("if (open) node.append(sessionBody(session))")
        self.assertLess(error, expanded)
        self.assertIn("Session error:", JS)
        self.assertRegex(CSS, r"\.session-error\s*\{")

    def test_snapshot_name_field_matches_the_server_limit(self):
        self.assertIn("maxlength: '120'", JS)

    def test_summary_capability_and_button_are_not_hardcoded_true(self):
        self.assertIn("data.capabilities.anthropic_api", JS)
        self.assertNotIn("=== 'anthropic-api' ? true", JS)
        self.assertIn("chunk.summary_eligible", JS)
        self.assertIn("disabled: !summaryAllowed", JS)


class SettingsSchemaTests(unittest.TestCase):
    """Every settings field must name a real config key (AUD-031's failure mode)."""

    def declared_paths(self) -> list[str]:
        block = re.search(r"const SETTINGS_SCHEMA = \[(.*?)\n\];", JS, re.S)
        self.assertIsNotNone(block, "SETTINGS_SCHEMA not found in app.js")
        return re.findall(r"path:\s*'([\w.]+)'", block.group(1))

    def test_every_field_maps_to_a_known_config_key(self):
        from vodpipe.schema import known_paths

        known = set(known_paths())
        unknown = [path for path in self.declared_paths() if path not in known]
        self.assertEqual(unknown, [],
                         f"settings fields with no schema entry: {unknown}")

    def test_no_field_is_declared_twice(self):
        paths = self.declared_paths()
        duplicates = sorted({path for path in paths if paths.count(path) > 1})
        self.assertEqual(duplicates, [])


if __name__ == "__main__":
    unittest.main()


class ChunkTableTests(unittest.TestCase):
    """The chunk table's header row and its cells must stay the same width.

    Adding an artifact column means touching two places that look nothing alike
    -- a list of header strings and a sequence of `el('td', ...)` calls -- and
    getting one of them wrong shifts every column after it without any error.
    """

    def row(self) -> str:
        start = JS.index("function sessionBody(")
        end = JS.index("el('thead'", start)
        return JS[start:end]

    def headers(self) -> list[str]:
        start = JS.index("el('thead'")
        block = JS[start:JS.index("]\n", start)]
        # The two element names the header row is built from are not columns.
        labels = re.findall(r"'([^']*)'", block)
        return [label for label in labels if label not in ("thead", "tr")]

    def test_the_header_and_the_cells_have_the_same_width(self):
        cells = len(re.findall(r"el\('td'", self.row()))
        self.assertEqual(cells, len(self.headers()),
                         f"{cells} cells against {self.headers()}")

    def test_every_artifact_status_the_state_model_carries_is_shown(self):
        """Read from the state model rather than a list written out here, so
        adding an artifact and forgetting the column is a failing test."""
        from vodpipe.state import Chunk

        shown = {field for field in vars(Chunk(index=0, session_id="s",
                                              channel="c", started_at=0.0))
                 if field.endswith("_status") or field == "status"}
        for field in sorted(shown):
            self.assertIn(f"chunk.{field}", self.row(), field)

    def test_the_retired_edit_column_is_gone(self):
        """This build records and transcribes; it does not cut. A column for an
        artifact nothing produces would sit at `pending` forever."""
        self.assertNotIn("Edit", self.headers())
        self.assertNotIn("edit_status", self.row())
