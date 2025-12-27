# CLAUDE.md

This file provides guidance for Claude when working on this codebase.

## Testing Requirements

- **Always add tests** for new features in the analyzer. Tests go in `tests/test_analyzer.py`.
- **Always use the Playwright MCP** to test any changes to the HTML report. Generate a report with test data and verify the UI works correctly.

## Releases

- **Always use the GitHub workflow** for releasing new versions. Do not create releases manually.

## Commit Messages

- **Always use `Fixes #<issue>` syntax** when fixing GitHub issues to auto-close them. Example:
  ```
  Fix tooltip display on mobile

  Fixes #42
  ```

## Project Structure

- `src/tally/` - Main source code
  - `analyzer.py` - Core analysis and HTML report generation
  - `merchant_utils.py` - Merchant normalization and rules
  - `format_parser.py` - CSV format parsing
- `tests/` - Test files
- `docs/` - Marketing website (GitHub Pages)
- `config/` - Example configuration files
