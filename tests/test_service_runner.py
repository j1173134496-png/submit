from pathlib import Path

from submit_flow_agent.service_runner import load_dotenv


def test_load_dotenv_supports_comments_export_and_quotes(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# ignored\nexport SUBMIT_TEST_ALPHA=one\nSUBMIT_TEST_BETA=\"two words\"\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SUBMIT_TEST_ALPHA", raising=False)
    monkeypatch.delenv("SUBMIT_TEST_BETA", raising=False)

    load_dotenv(env_path)

    assert __import__("os").environ["SUBMIT_TEST_ALPHA"] == "one"
    assert __import__("os").environ["SUBMIT_TEST_BETA"] == "two words"
