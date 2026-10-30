"""Tests for the check subcommand.

Every test uses --from-json so extraction never calls a model, and the --fix tests swap
the CLI's client for a scripted one, so nothing here touches the network either.
"""

import json
from pathlib import Path

import pypdfium2 as pdfium

from draftcheck.cli import EXIT_FAILED, EXIT_FINDINGS, EXIT_OK, format_table, main
from draftcheck.llm_client import JSONChatResult, Usage
from draftcheck.schema import Finding

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_JSON = REPO_ROOT / "tests" / "fixtures" / "plate_001.model.json"
SAMPLE_PDF = REPO_ROOT / "samples" / "generated" / "plate_001.pdf"


def run_cli(tmp_path, *extra: str) -> int:
    """Run `check` on the sample drawing with output going into tmp_path."""
    return main(
        [
            "check",
            str(SAMPLE_PDF),
            "--from-json",
            str(FIXTURE_JSON),
            "--out",
            str(tmp_path),
            *extra,
        ]
    )


def test_check_from_json_reports_the_seeded_error_and_exits_1(tmp_path, capsys):
    code = run_cli(tmp_path)

    out = capsys.readouterr().out
    assert code == EXIT_FINDINGS
    assert "GDT-001" in out
    assert "error" in out
    assert "model not called" in out


def test_check_writes_extraction_and_findings_but_no_raw(tmp_path):
    run_cli(tmp_path)

    extraction = json.loads((tmp_path / "extraction.json").read_text())
    findings = json.loads((tmp_path / "findings.json").read_text())

    assert extraction["title_block"]["drawing_number"] == "DC-0001"
    assert any(finding["rule_id"] == "GDT-001" for finding in findings)
    assert not (tmp_path / "raw.txt").exists(), "no model call means no raw reply to save"


def test_a_clean_drawing_exits_0(tmp_path, capsys):
    clean = json.loads(FIXTURE_JSON.read_text())
    # Give the frame's datum references symbols to point at, so GDT-001 has nothing to say.
    clean["datums"] = [
        {"label": label, "bbox": [0.1, 0.1, 0.2, 0.2]} for label in ("A", "B", "C")
    ]
    clean["fcfs"] = []
    clean["dimensions"] = [
        {"id": "dim_1", "value": 50, "is_basic": True, "bbox": [0.3, 0.3, 0.4, 0.4]}
    ]
    clean_path = tmp_path / "clean.json"
    clean_path.write_text(json.dumps(clean))

    code = main(
        ["check", str(SAMPLE_PDF), "--from-json", str(clean_path), "--out", str(tmp_path)]
    )

    assert code == EXIT_OK
    assert "No findings." in capsys.readouterr().out


def test_parse_issues_are_printed(tmp_path, capsys):
    data = json.loads(FIXTURE_JSON.read_text())
    data["fcfs"].append({"characteristic": "wobbliness", "tolerance": 0.1})
    bad_path = tmp_path / "with_bad_item.json"
    bad_path.write_text(json.dumps(data))

    main(["check", str(SAMPLE_PDF), "--from-json", str(bad_path), "--out", str(tmp_path)])

    assert "Parse issues" in capsys.readouterr().out


def test_a_missing_drawing_exits_2(tmp_path, capsys):
    code = main(
        [
            "check",
            str(tmp_path / "nope.pdf"),
            "--from-json",
            str(FIXTURE_JSON),
            "--out",
            str(tmp_path),
        ]
    )

    assert code == EXIT_FAILED
    assert "Could not read the drawing" in capsys.readouterr().err


def test_a_missing_extraction_file_exits_2(tmp_path, capsys):
    code = main(
        [
            "check",
            str(SAMPLE_PDF),
            "--from-json",
            str(tmp_path / "nope.json"),
            "--out",
            str(tmp_path),
        ]
    )

    assert code == EXIT_FAILED
    assert "Could not read" in capsys.readouterr().err


def test_unparsable_extraction_json_exits_2(tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")

    code = main(["check", str(SAMPLE_PDF), "--from-json", str(broken), "--out", str(tmp_path)])

    assert code == EXIT_FAILED
    assert "Could not read" in capsys.readouterr().err


def test_format_table_aligns_columns_and_puts_the_message_last():
    findings = [
        Finding(
            rule_id="GDT-001",
            severity="error",
            message="short",
            source="ASME Y14.5 — datums",
            item_ids=["F1", "F2"],
        ),
        Finding(
            rule_id="MFG-001",
            severity="warning",
            message="longer message here",
            source="shop practice",
            item_ids=["D3"],
        ),
    ]

    lines = format_table(findings).splitlines()

    assert lines[0].startswith("RULE")
    assert lines[1].endswith("short")
    assert "F1, F2" in lines[1]
    # Every row's message starts at the same column.
    assert lines[1].index("short") == lines[2].index("longer message here")


def test_format_table_on_no_findings():
    assert format_table([]) == "No findings."


class FixClient:
    """Stands in for LLMClient under --fix: one scripted correction, no network."""

    class _Provider:
        name = "scripted"
        model = "scripted-model"

    provider = _Provider()

    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat_json(self, system, user, images):
        edits = [
            {
                "op": "remove_datum_ref",
                "fcf_id": "fcf_1",
                "label": "C",
                "rationale": "Nothing on the sheet defines C, and two datums locate the plate.",
                "finding_ids": ["GDT-001@fcf_1"],
            },
            {"op": "remove_fcf", "id": "fcf_1"},
        ]
        return JSONChatResult(
            data={"edits": edits},
            text=json.dumps({"edits": edits}),
            latency_s=0.1,
            usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
            model="scripted-model",
            attempts=1,
        )


def use_fake_client(monkeypatch):
    """Point the CLI at FixClient and at config it does not need to read from .env."""
    import draftcheck.cli as cli_module

    monkeypatch.setattr(cli_module, "LLMClient", FixClient)
    monkeypatch.setattr(cli_module, "get_provider", lambda name=None: None)


def test_fix_runs_the_loop_and_prints_each_round(tmp_path, capsys, monkeypatch):
    use_fake_client(monkeypatch)

    run_cli(tmp_path, "--fix")

    out = capsys.readouterr().out
    assert "Correction loop" in out
    assert "Round 1: proposed 2, applied 1, rejected 1" in out
    assert "remove_datum_ref  datum C from frame fcf_1" in out
    assert "never deleted" in out, "the refused deletion and its reason should be printed"
    assert "resolved" in out


def test_fix_still_exits_1_because_the_corrections_are_unreviewed(tmp_path, capsys, monkeypatch):
    use_fake_client(monkeypatch)

    code = run_cli(tmp_path, "--fix")

    out = capsys.readouterr().out
    # The loop cleared the only error, and the run still fails: the fix is a proposal.
    assert "1 resolved, 0 unresolved" in out
    assert code == EXIT_FINDINGS
    assert "Agent proposals are unreviewed; original drawing has 1 error(s)." in out


def test_a_clean_drawing_under_fix_exits_0(tmp_path, capsys, monkeypatch):
    use_fake_client(monkeypatch)
    clean = json.loads(FIXTURE_JSON.read_text())
    clean["fcfs"] = []
    clean_path = tmp_path / "clean.json"
    clean_path.write_text(json.dumps(clean))

    code = main(
        [
            "check",
            str(SAMPLE_PDF),
            "--from-json",
            str(clean_path),
            "--out",
            str(tmp_path),
            "--fix",
        ]
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "Agent proposals are unreviewed; original drawing has 0 error(s)." in out


def test_fix_writes_corrections_json(tmp_path, monkeypatch):
    use_fake_client(monkeypatch)

    run_cli(tmp_path, "--fix")

    corrections = json.loads((tmp_path / "corrections.json").read_text())

    assert corrections["status"] == [
        {
            "finding": "GDT-001@fcf_1",
            "status": "resolved",
            "severity": "error",
            "message": corrections["status"][0]["message"],
        }
    ]
    assert corrections["final_findings"] == []
    assert len(corrections["final_drawing"]["fcfs"]) == 1, "the frame survived the loop"


def test_without_fix_the_model_is_never_called_for_corrections(tmp_path, capsys, monkeypatch):
    use_fake_client(monkeypatch)

    run_cli(tmp_path)

    assert "Correction loop" not in capsys.readouterr().out
    assert not (tmp_path / "corrections.json").exists()


def test_redline_writes_a_two_page_pdf_and_a_png_without_fix(tmp_path, capsys):
    run_cli(tmp_path, "--redline")

    out = capsys.readouterr().out
    assert "redline.pdf" in out and "redline.png" in out

    pdf = pdfium.PdfDocument(tmp_path / "redline.pdf")
    try:
        assert len(pdf) == 2
    finally:
        pdf.close()
    assert (tmp_path / "redline.png").stat().st_size > 0


def test_redline_works_together_with_fix(tmp_path, monkeypatch):
    use_fake_client(monkeypatch)

    code = run_cli(tmp_path, "--fix", "--redline")

    assert code == EXIT_FINDINGS
    assert (tmp_path / "redline.pdf").exists()
    assert (tmp_path / "corrections.json").exists()


def test_no_redline_flag_means_no_redline_files(tmp_path):
    run_cli(tmp_path)

    assert not (tmp_path / "redline.pdf").exists()
    assert not (tmp_path / "redline.png").exists()
