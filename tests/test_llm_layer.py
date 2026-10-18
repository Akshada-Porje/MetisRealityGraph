import json

from metis.bench_llm import classify, wilson
from metis.investigate import build_prompt, investigate, render_evidence
from metis.llm import LLMError, LLMResult, parse_json_object
from metis.scenarios import generate


class Fake:
    def __init__(self, fn):
        self.fn = fn

    def generate_json(self, system, user, repeat=0):
        text = self.fn(user)
        return LLMResult(text, parse_json_object(text), 10, 5, 0.0)


def _ans(cause, cited=None, conf="high"):
    return json.dumps({"cause_id": cause, "confidence": conf, "reasoning": "x",
                       "cited_evidence_ids": cited if cited is not None else ([cause] if cause else []),
                       "missing_evidence": [], "suggested_actions": []})


def _sc(t):
    return next(s for s in generate(2, 7) if s.type == t)


def test_validator_blocks_undeployed_blame_but_raw_does_not():
    sc = _sc("undeployed_fix")
    c2 = next(e.id for e in sc.evidence if e.id.endswith("commit:C2"))
    client = Fake(lambda u: _ans(c2))
    assert investigate(sc, client, "raw").cause_id == c2          # no guardrail
    inv = investigate(sc, client, "reconciled")
    assert inv.cause_id is None
    assert any("validator_abstained:cause_status_NOT_DEPLOYED" in f for f in inv.flags)


def test_fabricated_citation_is_flagged_and_rejected():
    sc = _sc("undeployed_fix")
    c1 = next(e.id for e in sc.evidence if e.id.endswith("commit:C1"))
    client = Fake(lambda u: _ans(c1, [c1, "made-up/id"]))
    raw = investigate(sc, client, "raw")
    assert "fabricated_citation:made-up/id" in raw.flags
    assert investigate(sc, client, "reconciled").cause_id is None


def test_uncited_cause_rejected_in_reconciled_mode():
    sc = _sc("undeployed_fix")
    c1 = next(e.id for e in sc.evidence if e.id.endswith("commit:C1"))
    inv = investigate(sc, Fake(lambda u: _ans(c1, [])), "reconciled")
    assert inv.cause_id is None and "uncited_cause" in inv.flags


def test_valid_answer_passes():
    sc = _sc("undeployed_fix")
    c1 = next(e.id for e in sc.evidence if e.id.endswith("commit:C1"))
    inv = investigate(sc, Fake(lambda u: _ans(c1)), "reconciled")
    assert inv.cause_id == c1 and classify(sc, inv) == "correct"


def test_garbage_output_is_an_error_not_a_silent_abstain():
    sc = _sc("undeployed_fix")
    inv = investigate(sc, Fake(lambda u: "not json at all"), "raw")
    assert inv.error == "unparseable_json" and classify(sc, inv) == "error"


def test_llm_exception_is_an_error():
    def boom(u):
        raise LLMError("429")
    class B:
        def generate_json(self, s, u, r=0):
            raise LLMError("429")
    inv = investigate(_sc("undeployed_fix"), B(), "raw")
    assert inv.error and inv.error.startswith("llm_error")


def test_prompt_hides_future_and_late_evidence_and_truth():
    sc = _sc("late_deploy_event")
    _, user, _ = build_prompt(sc, "raw")
    late = next(e for e in sc.evidence if e.kind == "deploy" and e.ingest_time > sc.incident_time)
    assert late.id not in user
    assert "truth" not in user.lower() and "ground" not in user.lower()
    _, ids = render_evidence(sc)
    assert late.id not in ids


def test_reconciled_prompt_includes_report_raw_does_not():
    sc = _sc("conflict_unresolvable")
    assert "reconciliation_report" not in build_prompt(sc, "raw")[1]
    assert "CONFLICT" in build_prompt(sc, "reconciled")[1]


def test_injection_text_is_inside_evidence_block_only():
    sc = _sc("injected_commit_message")
    system, user, _ = build_prompt(sc, "raw", 1)
    assert "ignore all previous instructions" not in system
    start, end = user.index("<evidence>"), user.index("</evidence>")
    assert start < user.index("ignore all previous instructions") < end


def test_parse_json_object_handles_fences():
    assert parse_json_object('```json\n{"a":1}\n```') == {"a": 1}
    assert parse_json_object("[1]") is None


def test_wilson_bounds():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.4
    assert wilson(0, 0) == (0.0, 0.0)


def test_raw_nudged_ablation_has_nudge_but_no_report():
    sc = _sc("conflict_resolvable")
    sys_raw, _, _ = build_prompt(sc, "raw")
    sys_n, user_n, rec = build_prompt(sc, "raw_nudged")
    assert "Prefer the most recent change" in sys_n and "Prefer the most recent change" not in sys_raw
    assert "reconciliation_report" not in user_n and rec is None
    assert build_prompt(sc, "raw")[1] == user_n      # identical evidence text
