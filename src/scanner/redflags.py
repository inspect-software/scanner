"""The red-flag registry: what a red flag is, and where each one's evidence lives.

A **red flag** is a finding that adjusts a rating downward instead of scoring
into it, and that a report presents as a named alert rather than as a number.
The methodology page states the class and the four rules every one of them
obeys; this module is the machine-readable half of that statement, and the
single place a new flag has to be declared.

It exists because the alternative was measured and failed. Each flag used to be
wired by hand into the scan writer, the rescorer, the catalogue summary, the
API, the cards and the aggregate — six places, none of which knew about the
others. `abandonment` shipped as a red flag and appeared in none of the
aggregate statistics for a day, because the code that collected them filtered
on a hand-maintained list of keys that nobody thought to extend. A registry
does not prevent someone forgetting to add a flag; it makes forgetting it
visible in one file instead of invisible in six.

Two storage shapes exist in the report, and both are read here so no consumer
has to know the difference:

- a **metric of its own** carrying ``inputs.red_flag`` — the metric's presence
  is what makes the flag assessable at all;
- a **state inside another metric's inputs**, where a distinguished value means
  "could not be determined". The Inorganic Growth Policy is this shape, because
  it is a factor over two components of ``popularity`` rather than a metric.

Adding a flag means adding one entry below. Nothing else in the scanner, the
website backend, or the catalogue needs to learn its name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

# A metric's inputs, as stored. Read defensively: reports written before a flag
# existed carry nothing for it, and that is "not assessed", never "clean".
Inputs = dict


@dataclass(frozen=True)
class RedFlag:
    """One declared red flag.

    ``key`` names the *finding*, not the metric it is stored in — the two
    differ for growth authenticity, and a consumer should never have to know
    where the evidence happens to sit.
    """

    key: str
    metric_key: str
    name: str
    wiki_slug: str
    read: Callable[[Inputs], tuple[bool, bool]]
    """inputs -> (assessed, flagged). ``assessed`` False means the report could
    not answer the question, which is not the same as answering it cleanly."""


def _red_flag_input(inputs: Inputs) -> tuple[bool, bool]:
    """The ordinary shape: a metric whose inputs carry an explicit boolean.

    The metric exists only where its evidence could be collected, so its
    presence *is* the assessment having happened.
    """
    value = inputs.get("red_flag")
    return value is not None, value is True


GROWTH_FLAGGED_STATES = ("anomalous", "highly_anomalous")
GROWTH_ASSESSED_STATES = ("organic", *GROWTH_FLAGGED_STATES)


def _growth_state(inputs: Inputs) -> tuple[bool, bool]:
    """The Inorganic Growth Policy's four-state field.

    ``unverified`` is the whole reason this shape exists: history collection is
    bounded, so most reports cannot answer the question at all, and counting
    them as clean would claim something the evidence never said.
    """
    state = inputs.get("growth_state")
    return state in GROWTH_ASSESSED_STATES, state in GROWTH_FLAGGED_STATES


REGISTRY: tuple[RedFlag, ...] = (
    RedFlag(
        key="abandonment",
        metric_key="abandonment",
        name="Abandonment",
        wiki_slug="abandonment",
        read=_red_flag_input,
    ),
    RedFlag(
        key="malicious_dependencies",
        metric_key="malicious_dependencies",
        name="Malicious dependencies",
        wiki_slug="malicious-dependencies",
        read=_red_flag_input,
    ),
    RedFlag(
        key="high_risk_jurisdiction_exposure",
        metric_key="high_risk_jurisdiction_exposure",
        name="High-risk jurisdiction exposure",
        wiki_slug="jurisdiction-exposure",
        read=_red_flag_input,
    ),
    RedFlag(
        key="inorganic_growth",
        metric_key="popularity",
        name="Inorganic growth",
        wiki_slug="growth-authenticity",
        read=_growth_state,
    ),
)

BY_KEY: dict[str, RedFlag] = {flag.key: flag for flag in REGISTRY}


@dataclass(frozen=True)
class RedFlagFinding:
    """What one flag says about one report."""

    flag: RedFlag
    assessed: bool
    flagged: bool


def assess(metrics) -> list[RedFlagFinding]:
    """Every declared flag's verdict for a report's metrics.

    Accepts anything with a ``by_key`` lookup returning metrics that carry
    ``inputs`` — the scanner's ``Metrics`` model, or a rescored one. A flag
    whose metric is absent reads as not assessed rather than being omitted, so
    a caller can always tell "no finding" from "no evidence".
    """
    findings: list[RedFlagFinding] = []
    for flag in REGISTRY:
        metric = metrics.by_key(flag.metric_key) if metrics is not None else None
        inputs = getattr(metric, "inputs", None) or {}
        assessed, flagged = flag.read(inputs)
        findings.append(RedFlagFinding(flag=flag, assessed=assessed, flagged=flagged))
    return findings


def flagged_keys(metrics) -> list[str]:
    """Keys of the flags a report actually raises, in registry order.

    The catalogue's summary of a repository: short, stable, and complete
    without a column per flag.
    """
    return [f.flag.key for f in assess(metrics) if f.flagged]
