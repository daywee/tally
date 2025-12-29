"""End-to-end smoke tests for tally CLI.

These tests run the full tally workflow from init to report generation,
testing on all supported platforms (Windows, macOS, Linux).
"""

import pytest
import subprocess
import os
from pathlib import Path


def run_tally(args: list, cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run tally command and return result."""
    result = subprocess.run(
        ['uv', 'run', 'tally'] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding='utf-8'
    )
    if check and result.returncode != 0:
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
    return result


class TestEndToEnd:
    """End-to-end smoke tests for the full tally workflow."""

    def test_full_workflow(self, tmp_path):
        """Test complete workflow: init -> add data -> discover -> run -> explain."""
        workdir = tmp_path / "budget"
        workdir.mkdir()

        # 1. Run tally init
        result = run_tally(['init'], str(workdir))
        assert result.returncode == 0
        assert 'Created:' in result.stdout
        assert 'NEXT STEPS' in result.stdout
        assert 'LET AI DO THE WORK' in result.stdout

        # Verify init created expected structure
        tally_dir = workdir / 'tally'
        assert (tally_dir / 'config' / 'settings.yaml').exists()
        assert (tally_dir / 'config' / 'merchant_categories.csv').exists()
        assert (tally_dir / 'AGENTS.md').exists()
        assert (tally_dir / 'CLAUDE.md').exists()

        # 2. Create test transaction data
        data_dir = tally_dir / 'data'
        data_dir.mkdir(exist_ok=True)

        # Create fake AMEX-style CSV
        csv_content = """Date,Description,Amount
01/15/2025,NETFLIX.COM,-15.99
01/16/2025,AMAZON.COM*ABC123,-45.50
01/17/2025,STARBUCKS STORE 12345,-6.75
01/18/2025,UBER *TRIP,-25.00
01/20/2025,WHOLE FOODS MARKET,-89.23
02/01/2025,NETFLIX.COM,-15.99
02/05/2025,SPOTIFY USA,-9.99
02/10/2025,TARGET 00012345,-156.78
02/15/2025,SHELL OIL 123456,-52.30
03/01/2025,NETFLIX.COM,-15.99
03/05/2025,COSTCO WHSE,-234.56
03/10/2025,DOORDASH*CHIPOTLE,-32.45
"""
        (data_dir / 'transactions.csv').write_text(csv_content, encoding='utf-8')

        # 3. Update settings to use our test data
        settings_content = """year: 2025
data_sources:
  - name: TestBank
    file: data/transactions.csv
    format: "{date:%m/%d/%Y},{description},{amount}"
"""
        (tally_dir / 'config' / 'settings.yaml').write_text(settings_content, encoding='utf-8')

        # 4. Add some merchant rules
        rules_content = """Pattern,Merchant,Category,Subcategory
NETFLIX,Netflix,Subscriptions,Streaming
SPOTIFY,Spotify,Subscriptions,Streaming
AMAZON,Amazon,Shopping,Online
STARBUCKS,Starbucks,Food,Coffee
UBER,Uber,Transport,Rideshare
WHOLE FOODS,Whole Foods,Food,Grocery
TARGET,Target,Shopping,Retail
SHELL OIL,Shell,Transport,Gas
COSTCO,Costco,Shopping,Wholesale
DOORDASH,DoorDash,Food,Delivery
"""
        (tally_dir / 'config' / 'merchant_categories.csv').write_text(rules_content, encoding='utf-8')

        # 5. Run tally discover - should find no unknown merchants
        result = run_tally(['discover'], str(tally_dir))
        assert result.returncode == 0
        assert 'No unknown transactions' in result.stdout or 'All merchants are categorized' in result.stdout

        # 6. Run tally diag - verify config is valid
        result = run_tally(['diag'], str(tally_dir))
        assert result.returncode == 0
        assert 'CONFIGURATION' in result.stdout
        assert 'TestBank' in result.stdout
        assert 'MERCHANT RULES' in result.stdout

        # 7. Run tally run --summary - generate analysis
        result = run_tally(['run', '--summary'], str(tally_dir))
        assert result.returncode == 0
        assert '2025 SPENDING ANALYSIS' in result.stdout
        assert 'Netflix' in result.stdout

        # 8. Run tally run - generate HTML report
        result = run_tally(['run'], str(tally_dir))
        assert result.returncode == 0
        assert 'HTML report:' in result.stdout

        # Verify HTML was created
        output_dir = tally_dir / 'output'
        html_files = list(output_dir.glob('*.html'))
        assert len(html_files) == 1
        html_content = html_files[0].read_text(encoding='utf-8')
        assert 'Spending Report' in html_content
        assert 'Netflix' in html_content

        # 9. Run tally explain - verify merchant classification
        result = run_tally(['explain', 'Netflix'], str(tally_dir))
        assert result.returncode == 0
        assert 'Netflix' in result.stdout
        assert 'Monthly' in result.stdout or 'Subscriptions' in result.stdout

        # 10. Run tally version
        result = run_tally(['version'], str(tally_dir))
        assert result.returncode == 0
        assert 'tally' in result.stdout.lower() or 'version' in result.stdout.lower()

    def test_init_creates_valid_structure(self, tmp_path):
        """Test that tally init creates a valid, runnable structure."""
        workdir = tmp_path / "test_init"
        workdir.mkdir()

        # Run init
        result = run_tally(['init'], str(workdir))
        assert result.returncode == 0

        tally_dir = workdir / 'tally'

        # Verify all expected files exist
        expected_files = [
            'config/settings.yaml',
            'config/merchant_categories.csv',
            '.gitignore',
            'README.md',
            'AGENTS.md',
            'CLAUDE.md',
        ]
        for filepath in expected_files:
            assert (tally_dir / filepath).exists(), f"Missing: {filepath}"

        # Verify settings.yaml is valid YAML
        import yaml
        settings = yaml.safe_load((tally_dir / 'config' / 'settings.yaml').read_text(encoding='utf-8'))
        assert 'year' in settings
        assert 'data_sources' in settings

    def test_discover_finds_unknown_merchants(self, tmp_path):
        """Test that discover correctly identifies uncategorized merchants."""
        workdir = tmp_path / "test_discover"
        workdir.mkdir()

        # Set up minimal config with unknown merchant
        config_dir = workdir / 'config'
        data_dir = workdir / 'data'
        config_dir.mkdir()
        data_dir.mkdir()

        # Settings
        (config_dir / 'settings.yaml').write_text("""year: 2025
data_sources:
  - name: Test
    file: data/test.csv
    format: "{date:%m/%d/%Y},{description},{amount}"
""", encoding='utf-8')

        # Empty merchant rules (header only)
        (config_dir / 'merchant_categories.csv').write_text(
            "Pattern,Merchant,Category,Subcategory\n", encoding='utf-8'
        )

        # Transaction with unknown merchant
        (data_dir / 'test.csv').write_text("""date,description,amount
01/15/2025,MYSTERY STORE 123,-99.99
""", encoding='utf-8')

        # Run discover
        result = run_tally(['discover'], str(workdir))
        assert result.returncode == 0
        assert 'MYSTERY' in result.stdout.upper() or 'unknown' in result.stdout.lower()

    def test_run_with_no_data_sources_shows_error(self, tmp_path):
        """Test that run without data sources gives helpful error."""
        workdir = tmp_path / "test_no_data"
        workdir.mkdir()

        # Run init
        run_tally(['init'], str(workdir))
        tally_dir = workdir / 'tally'

        # Run without adding data - should error helpfully
        result = run_tally(['run'], str(tally_dir), check=False)
        assert result.returncode == 1
        assert 'data_sources' in result.stderr.lower() or 'no data' in result.stderr.lower()

    def test_external_assets_mode(self, tmp_path):
        """Test --no-embedded-html creates separate asset files."""
        workdir = tmp_path / "test_external"
        workdir.mkdir()

        # Run init
        run_tally(['init'], str(workdir))
        tally_dir = workdir / 'tally'

        # Add minimal data
        data_dir = tally_dir / 'data'
        data_dir.mkdir(exist_ok=True)
        (data_dir / 'test.csv').write_text("""date,description,amount
01/15/2025,TEST MERCHANT,-50.00
""", encoding='utf-8')

        (tally_dir / 'config' / 'settings.yaml').write_text("""year: 2025
data_sources:
  - name: Test
    file: data/test.csv
    format: "{date:%m/%d/%Y},{description},{amount}"
""", encoding='utf-8')

        (tally_dir / 'config' / 'merchant_categories.csv').write_text(
            "Pattern,Merchant,Category,Subcategory\nTEST,Test,Shopping,Retail\n", encoding='utf-8'
        )

        # Run with --no-embedded-html
        result = run_tally(['run', '--no-embedded-html'], str(tally_dir))
        assert result.returncode == 0

        # Verify separate files were created
        output_dir = tally_dir / 'output'
        assert (output_dir / 'spending_report.css').exists()
        assert (output_dir / 'spending_report.js').exists()
        assert (output_dir / 'spending_data.js').exists()

        # Verify HTML references external files
        html_files = list(output_dir.glob('*.html'))
        assert len(html_files) == 1
        html_content = html_files[0].read_text(encoding='utf-8')
        assert 'href="spending_report.css"' in html_content
        assert 'src="spending_report.js"' in html_content
