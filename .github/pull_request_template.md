## What this changes

<!-- One paragraph. If it fixes an issue, link it. -->

## Why

<!-- What went wrong, or what was impossible before. The "why" is what gets written into
     the code comments in this project, so it matters. -->

## Checklist

- [ ] `python -m unittest discover -s tests -t .` passes in full
- [ ] New behaviour has a test; a bug fix has a test that fails without the fix
- [ ] No new third-party dependencies
- [ ] If a config key, manifest field or published file was **removed**, it is retired
      (`schema.RETIRED_PATHS` / `state._RETIRED_CHUNK_FIELDS` / `exports.RETIRED_EDIT_EXPORTS`)
      and `tests/test_retirement.py` covers it
- [ ] If it changes documented behaviour, `README.md` / `CLAUDE.md` / `DESIGN.md` are updated
- [ ] Nothing added can lose a recording, master or transcript
