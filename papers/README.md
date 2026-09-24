# IQX White Paper v0.1

**IQX: Forward-Only Reputation and Trust Routing for AI Agents**

Xin Jin, IQX Labs · v0.1 — Editorial revision 1, 23 September 2026

**[Read the paper (PDF, 37 pages)](iqx-whitepaper-v0.1.pdf)**

This is a technical report. It has not been peer reviewed.

## Revision

This is editorial revision 1 of the edition of 22 September 2026. It clarifies
the paper's motivation and the contribution of its first stage, in the
abstract, a new section 1.1, section 16.6 and the conclusion, and it renames
"How to read this paper" to "Scope and interpretation". **Experimental
results, methods, and verdicts are unchanged.** No number, interval,
threshold, stopping rule or limitation differs from the edition of
22 September 2026.

The edition of 22 September 2026 remains available at commit `2edc6ba` of
this repository:
[iqx-whitepaper-v0.1.pdf at 2edc6ba](https://github.com/open-iqx/iqx-protocol/blob/2edc6ba39e8cf21060c83a878bd3f4428e840e21/papers/iqx-whitepaper-v0.1.pdf).

## Summary

IQX is an experimental protocol for accumulating forward-only, outcome-backed
evidence about AI agents. A publisher creates a falsifiable, time-bounded task,
an identified agent commits an answer before the outcome exists, and a verifier
later grades that answer against the outcome. Tasks, agent versions, answers
and outcomes are kept as separate records, so that a performance record can be
audited and recomputed. An agent that changes materially enters under a new
identity instead of inheriting its predecessor's history.

The paper reports a preliminary prospective study in one arena, a
delayed-outcome DeFi market domain. The first result there was negative: a
legacy Elo rule rewarded the majority class, and class-balanced informedness
placed the observed agents near zero. The rule was corrected by adopting that
established measure. The next question was whether candidate agents could show
positive prospective informedness beyond constant-answer baselines. The current
experiment, V3.3, pairs a market-only agent with a wallet-aware agent that
differs from it only by a wallet-derived term. Both answer the same tasks from
the same market capture. Its verdicts are governed by two more specific
preregistered questions: whether the task family is learnable (Q1), and,
separately, whether the wallet term adds value beyond the market (Q2).

Across 239 settled paired tasks, both V3 predictors had positive overall
informedness, with reported 95 % intervals wholly above zero. This is a
descriptive result. It does not establish passage of the complete
preregistered capability gates, cross-epoch stability, superiority to
momentum, trading profitability, or routing value. Under the preregistered
rules:

- **Q1** (is the task family learnable?) is **not established**, and no
  capability tier is claimed.
- **Q2** (does wallet information add value beyond the market?) is
  **inconclusive**. The increment is not demonstrated, and that is not evidence
  of absence.
- The evidence ends at an **administrative cut**. The preregistered stopping
  rule would have continued.
- **Predictive reputation and routing value remain unvalidated.**

This first stage contributes an operational, auditable forward-evaluation
system, run in one live arena; a documented failure and correction of its
original scoring rule; and a preliminary prospective paired study in which both
candidate agents showed positive pooled informedness. That the system runs does
not by itself show that its evaluation method is valid, and the agents' scores
do not validate it.

## Citation

> Xin Jin. 2026. *IQX: Forward-Only Reputation and Trust Routing for AI Agents.*
> Technical report, White Paper v0.1, editorial revision 1, 23 September 2026
> (edition of 22 September 2026). IQX Labs. Not peer reviewed.
> <https://github.com/open-iqx/iqx-protocol>

```bibtex
@techreport{jin2026iqx,
  author      = {Jin, Xin},
  title       = {{IQX}: Forward-Only Reputation and Trust Routing for {AI} Agents},
  institution = {IQX Labs},
  type        = {Technical report},
  number      = {White Paper v0.1},
  year        = {2026},
  month       = sep,
  note        = {Editorial revision 1, 23 September 2026 (edition of 22 September 2026). Not peer reviewed},
  url         = {https://github.com/open-iqx/iqx-protocol}
}
```

The report has no DOI and no arXiv identifier.

## File integrity

```
da64c558ca938c153978c15d8ca452d2c4f547432ba4f77f0f256263a788f649  iqx-whitepaper-v0.1.pdf
```

This is the SHA-256 of the PDF of editorial revision 1. Check it with
`shasum -a 256` or `sha256sum`. The PDF of the edition of 22 September 2026,
at commit `2edc6ba`, has SHA-256
`e3d9730a52cc26ebe4b7d58add873d92c04bfe1d569179340b357ea555bde8ba`. The digests
printed inside the paper identify the evidence and analyses it reports, not the
PDF.

## License

© 2026 Xin Jin. The paper is licensed under the
[Creative Commons Attribution 4.0 International License (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).
The full legal text is in [LICENSE](LICENSE).

The license covers the paper, `iqx-whitepaper-v0.1.pdf`, including its three
figures, which were made for it. It does not cover:

- the software in this repository, including the `iqx` package, which remains
  under the Apache License 2.0 in the [root LICENSE](../LICENSE);
- the works the paper cites, which it references but does not reproduce;
- the Latin Modern fonts embedded in the PDF, which are distributed under their
  own license, the GUST Font License.

## Code and data availability

The public artifact of this work is the protocol reference SDK in this
repository: the `iqx` package, version 0.1.1, tag `v0.1.1`, commit
`1721acff34348b1f5c89661e6859c2a1b27b493c`. It is a protocol implementation
with example agents. It contains no V3 predictor, no wallet-reliability term,
and no readout or other research tooling. Adding the paper changes nothing in
the package.

The V3 research implementation, its analysis and readout tooling, the frozen
evidence archive, the preregistrations and the internal records the paper lists
are not public. The digests the paper publishes identify those artifacts. They
are provenance identifiers, not a way to obtain the artifacts, so the paper's
experiments cannot be reproduced from this repository. The paper makes no
commitment to release the private materials.
