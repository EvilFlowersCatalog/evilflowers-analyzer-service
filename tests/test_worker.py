import worker


def test_every_dispatched_job_is_declared():
    assert worker.JOBS == ["paragraphs", "figures", "equations", "tables"]
    assert worker.extract.name == "evilflowers_analyzer.extract"
