from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


def test_unmarked_run_omits_optional_integrity_field_on_real_http(artifacts):
    _, client, meeting, rid, _ = artifacts
    run = client.get(f"/api/runs/{rid}")
    assert run.status_code == 200, run.text
    assert "report_unavailable_reason" not in run.json()
    snapshot = client.get(f"/api/runs/{rid}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert "report_unavailable_reason" not in snapshot.json()["run"]
    meeting_view = client.get(f"/api/meetings/{meeting}")
    assert meeting_view.status_code == 200, meeting_view.text
    assert all(
        "report_unavailable_reason" not in run for run in meeting_view.json()["runs"]
    )
