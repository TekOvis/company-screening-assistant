"""The evaluation runner: mock runs end to end; live runs never start by accident."""

import json

from screener import evaluate, metrics
from screener.fake_llm import FakeAnthropicClient


def test_mock_evaluation_runs_end_to_end(monkeypatch, tmp_path):
    # The web app's per-run cap must not apply: each case is its own screening.
    # (The first real run shared a 60-request run cap and stopped after 14 cases.)
    monkeypatch.setenv("MAX_LLM_REQUESTS_PER_RUN", "10")
    assert evaluate.main(["--mode", "mock", "--out", str(tmp_path)]) == 0

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert "budget_exceeded" not in report["statuses"]
    per_case = report["limits"]["max_llm_requests_per_company"]
    assert report["eval_caps"]["max_requests"] == 20 * per_case
    assert report["mode"] == "mock" and "MOCK MODE" in report["notice"]
    assert report["dataset"] == {
        "path": "eval/cases",
        "cases": 20,
        "synthetic": True,
        "sha256": report["dataset"]["sha256"],
    }
    assert report["cost"]["usd"] == 0.0
    final = report["classification"]["final"]
    assert set(final["confusion_matrix"]) == set(metrics.LABELS)
    assert sum(sum(row.values()) for row in final["confusion_matrix"].values()) == 20

    lines = (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20
    redirect_case = json.loads(lines[19])
    assert redirect_case["id"] == "20-bakery-insufficient-offsite-redirect"
    assert all("lure.example" not in page for page in redirect_case["result"]["pages_fetched"])
    assert "MOCK MODE" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_api_mode_needs_a_key(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert evaluate.main(["--mode", "api", "--only", "01-valves-fit-homepage"]) == 1
    assert "Nothing was sent" in capsys.readouterr().out


def test_api_mode_does_nothing_without_yes(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")

    def refuse(settings):
        raise AssertionError("a real client must not be created without --yes")

    monkeypatch.setattr(evaluate, "create_client", refuse)
    out_dir = tmp_path / "run"
    code = evaluate.main(
        ["--mode", "api", "--only", "01-valves-fit-homepage", "--out", str(out_dir)]
    )
    output = capsys.readouterr().out
    assert code == 2
    assert "Hard caps for this run" in output and "--yes" in output
    assert not out_dir.exists()


def test_api_branch_with_yes_and_a_stand_in_client(monkeypatch, tmp_path):
    """Exercises the API code path (spending plan, cost at tariffs) without paying."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(evaluate, "create_client", lambda settings: FakeAnthropicClient())
    args = ["--mode", "api", "--only", "01-valves-fit-homepage", "--yes", "--out", str(tmp_path)]
    assert evaluate.main(args) == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "api" and report["backend"] == "anthropic"
    assert "billed per token" in report["billing"] and "synthetic" in report["notice"]
    assert report["model"] == "claude-sonnet-5-5 via Anthropic API"
    assert report["cost"]["usd"] > 0  # computed from the reported tokens at listed tariffs


def test_unknown_case_ids_are_rejected(capsys):
    assert evaluate.main(["--mode", "mock", "--only", "99-missing"]) == 1
    assert "Unknown case ids" in capsys.readouterr().out
