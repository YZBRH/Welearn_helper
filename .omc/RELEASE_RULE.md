# Release Rules
<!-- last-analyzed: 2026-11-01T00:00:00+08:00 -->

## Version Sources
- `version.py` — `__version__`
- `main.py` — banner imports `version.__version__`
- `web_gui.py` — banner imports `version.__version__`

## Release Trigger
- Tag push matching `v*` starts `.github/workflows/release.yml`.
- Local/manual executable build: `powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1`.

## Test Gate
- `python -m pytest`
- CI runs tests before building and uploading the executable.

## Registry / Distribution
- No package registry publication.
- GitHub Release asset: `dist/WelearnHelper.exe` and SHA-256 checksum, created by `softprops/action-gh-release@v2`.

## Release Notes Strategy
- `README.md` contains a dated Chinese changelog.
- Release notes are manually prepared in the GitHub Release; no separate changelog file.

## CI Workflow Files
- `.github/workflows/release.yml`

## First-Time Setup Gaps
- The repository previously had no semver tags. Use annotated tags in the form `vX.Y.Z`.
