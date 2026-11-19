
import threading
from datetime import datetime
from pathlib import Path

import automator.workers.run_features_worker as worker


def test_consolidated_csv_failure_preserves_session_csv(
    tmp_path, monkeypatch, capsys
):
    session_dir = tmp_path / "session"
    blocked_path = tmp_path / "blocked"

    # Create a file where the consolidated log directory
    # should exist, causing directory creation to fail.
    blocked_path.write_text("blocked", encoding="utf-8")

    original_storage = worker.STORAGE

    class TestStorage:
        def path(self, key):
            if key == "feature_runs":
                return blocked_path
            return original_storage.path(key)

    monkeypatch.setattr(worker, "STORAGE", TestStorage())

    now = datetime(2026, 11, 17, 12, 0, 0)
    result = worker.RunResult(
        feature="12345",
        status="success",
        message="Completed successfully",
        started_at=now,
        duration_sec=1.0,
        duration_hms="00:00:01",
        sqlite_copied=False,
        timestamp=now,
        stderr=None,
    )

    worker._write_csv_row(
        threading.Lock(),
        session_dir,
        result,
    )

    session_csvs = list(session_dir.glob("*.csv"))

    assert len(session_csvs) == 1
    assert "12345" in session_csvs[0].read_text(
        encoding="utf-8"
    )

    captured = capsys.readouterr()

    assert "WARNING: Could not update consolidated run CSV" in captured.err
    assert result.status == "success"
