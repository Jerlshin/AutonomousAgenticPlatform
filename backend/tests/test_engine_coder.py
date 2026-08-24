"""The Coder's revision mode (AGENTS.md §7.3).

The correctness loop only works if the diagnosis actually reaches the next attempt, and
reaches it as a *patch instruction* rather than as a fresh brief. A Coder handed only "here
is what went wrong" rewrites from scratch and reintroduces bugs in the parts that already
worked — which is how a debug loop becomes a random walk that exhausts its budget without
converging.
"""

from __future__ import annotations

from app.engine.nodes.coder import _revision_block
from app.engine.state import CodeRevision, Diagnosis, ErrorKind, ErrorRecord

PREVIOUS = CodeRevision(
    revision=1,
    content="df = pd.read_parquet(path)\ny = df['target']\n",
    sha256="a" * 64,
)

ERROR = ErrorRecord(
    kind=ErrorKind.DATA,
    fingerprint="KeyError:target",
    exception_type="KeyError",
    message="'target'",
    traceback="Traceback (most recent call last):\nKeyError: 'target'\n",
    file="/workspace/main.py",
    line=12,
    offending_source="11   df = pd.read_parquet(path)\n12 > y = df['target']",
    revision=1,
)


def diagnosis(**overrides) -> Diagnosis:
    base = {
        "error_fingerprint": "KeyError:target",
        "root_cause": "the parquet file names the label column `diagnosis`",
        "evidence": ["KeyError: 'target'"],
        "fix_strategy": "read the column the file actually has",
        "targeted_changes": [
            "Replace `df['target']` with `df['diagnosis']` on line 12."
        ],
        "confidence": 0.85,
    }
    return Diagnosis(**{**base, **overrides})


def state(**overrides) -> dict:
    base = {
        "current_revision": PREVIOUS,
        "last_error": ERROR,
        "last_diagnosis": diagnosis(),
    }
    base.update(overrides)
    return base


class TestRevisionBlock:
    def test_the_first_attempt_has_no_revision_block(self):
        assert _revision_block({}) == ""
        assert _revision_block({"current_revision": PREVIOUS}) == ""

    def test_the_block_carries_the_previous_code_and_the_directive(self):
        block = _revision_block(state())
        assert "this is revision 2" in block
        assert "y = df['target']" in block  # the previous program
        assert "names the label column `diagnosis`" in block
        assert "Replace `df['target']`" in block

    def test_the_traceback_is_fenced_as_untrusted(self):
        """Program output is evidence for the Coder too, never instruction."""
        assert '<untrusted source="sandbox_stderr"' in _revision_block(state())

    def test_the_failing_region_is_shown_with_its_line_number(self):
        block = _revision_block(state())
        assert "Failing source region (line 12)" in block
        assert "12 > y = df['target']" in block

    def test_the_sidecar_is_told_which_fingerprint_this_revision_answers(self):
        assert 'Set `addresses_error` to "KeyError:target"' in _revision_block(state())

    def test_the_rule_against_rewriting_is_stated(self):
        """Every rewrite risks new bugs in code that was already correct."""
        assert "Do not rewrite working code" in _revision_block(state())

    def test_prior_art_from_earlier_runs_is_passed_through(self):
        block = _revision_block(
            state(
                last_diagnosis=diagnosis(
                    prior_art=["Rename `target` to `diagnosis` — fixed in run 91c2."]
                )
            )
        )
        assert "fixed in run 91c2" in block

    def test_a_low_confidence_diagnosis_tells_the_coder_to_trust_the_traceback(self):
        """A degraded Debugger's guess must not be followed as if it were analysis."""
        block = _revision_block(state(last_diagnosis=diagnosis(confidence=0.1)))
        assert "low-confidence (0.10)" in block
        assert "prefer your own reading" in block

    def test_a_confident_diagnosis_carries_no_such_caveat(self):
        assert "low-confidence" not in _revision_block(state())

    def test_an_error_with_no_source_region_still_produces_a_block(self):
        block = _revision_block(
            state(last_error=ERROR.model_copy(update={"offending_source": None}))
        )
        assert "Failing source region" not in block
        assert "### Diagnosis" in block
