from app.scheduled_synthesis import compare_packet


def packet(version=10, confidence="low", evidence=(1,)):
    return {"cluster_id": 3, "observations": [{"id": i} for i in evidence],
            "prior_assessments": [{"id": version, "key": "one", "scenario": "Claim unresolved",
                                   "confidence": confidence, "rationale": "Attribution uncertain"}]}


def test_identical_hypothesis_version_does_not_count_as_material_change():
    baseline = compare_packet(packet(), None, {1}, set())
    assert baseline["baseline_initialized"]
    assert compare_packet(packet(version=11), baseline, {1}, set())["outcome"] == "no_material_change"
    delta = compare_packet(packet(version=11, confidence="medium"), baseline, {1}, set())
    assert delta["outcome"] == "changed"
    assert delta["assessment_deltas"][0]["before"]["confidence"] == "low"


def test_substantive_unreviewed_evidence_persists_across_cycles():
    baseline = compare_packet(packet(), None, {1}, set())
    new = packet(evidence=(1, 2, 3))
    delta = compare_packet(new, baseline, {1}, {3})
    assert delta["outcome"] == "review_required"
    assert delta["pending_review_ids"] == [2]
    assert compare_packet(new, delta, {1}, {3})["pending_review_ids"] == [2]
    assert compare_packet(new, delta, {1, 2}, {3})["outcome"] == "no_material_change"
