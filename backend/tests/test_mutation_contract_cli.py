"""The contract CLI writes and checks only its selected output directory."""

import sys
from pathlib import Path

import pytest
from round_robin import export_contracts


def test_contract_cli_defaults_to_repository_contracts_directory(monkeypatch):
    destinations = []
    monkeypatch.setattr(export_contracts, "export", destinations.append)
    monkeypatch.setattr(sys, "argv", ["export-contracts"])

    export_contracts.main()

    assert destinations == [Path(__file__).resolve().parents[2] / "contracts"]


def test_contract_cli_exports_then_detects_drift_without_rewriting(tmp_path, monkeypatch):
    output = tmp_path / "exported-contracts"
    monkeypatch.setattr(sys, "argv", ["export-contracts", "--output", str(output)])
    export_contracts.main()
    assert (output / "brief.schema.json").is_file()
    assert (output / "sample.json").is_file()

    monkeypatch.setattr(sys, "argv", ["export-contracts", "--output", str(output), "--check"])
    with pytest.raises(SystemExit) as success:
        export_contracts.main()
    assert success.value.code == 0

    artifact = output / "brief.schema.json"
    artifact.write_text("modified", encoding="utf-8")
    with pytest.raises(SystemExit) as failure:
        export_contracts.main()
    assert failure.value.code == 1
    assert artifact.read_text(encoding="utf-8") == "modified"
