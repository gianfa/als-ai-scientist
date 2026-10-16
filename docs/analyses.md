# The two experiments the system can run itself

**In one paragraph.** The system invents hypotheses about ALS and proposes experiments that could prove them
wrong. Most of those experiments need a laboratory, so the system can only propose them, not run them. Two
experiments are different: they are small computer analyses of data we already have, written in advance, which
the system is allowed to run on its own. Think of two measuring instruments on a shelf: an agent can pick one up
and press the button, but it cannot change how the instrument measures, and it cannot argue with the reading
afterwards.

Both instruments work on data from one study: Michaelis *et al.*, *Nature* 2025, which found that people with
ALS have T cells (a kind of immune cell) that react against the C9orf72 protein [1].

| | `hla_panel_compact_v1` | `ics_cytokine_skew_v1` |
|---|---|---|
| Asks | Can a few protein fragments reach every immune type in the panel? | Do ALS patients' reacting T cells favour a different signal than healthy donors'? |
| Works on | computer predictions | lab measurements of real cells |
| Code | [`analysis/experiment_hla_panel.py`](../analysis/experiment_hla_panel.py) | [`analysis/experiment_ics_cytokines.py`](../analysis/experiment_ics_cytokines.py) |

How an experiment reaches either of them, and what happens to the result afterwards, is in
[pipeline.md](pipeline.md). Where the input files come from is in [data-acquisition.md](data-acquisition.md).

---

## 1. `hla_panel_compact_v1`: HLA allele-panel coverage

### Definition

A T cell only reacts to a protein fragment (a *peptide*) when one of the person's own immune molecules, an
*HLA allele*, holds that fragment up for it to see. Different people carry different HLA alleles. This analysis
asks a narrow, computable question: of the C9orf72 peptides that T cells recognised in the study, what is the
smallest group that is *predicted* to be held up by every HLA allele in the study's four-allele panel?

### Input

- The 21 recognised C9orf72 peptides (Table 1 of Michaelis *et al.* [1]).
- The 4 HLA class II alleles of the study's panel (Table 2 of [1]).
- A cache of binding predictions for every peptide-allele pair, computed by NetMHCIIpan-4.0 [2] through the
  IEDB web service (see [data-acquisition.md §5](data-acquisition.md#5-iedb--netmhciipan-prediction-cache-scripted)).

### Output

- The smallest group of peptides (at most 8) that is predicted to cover every allele, and which alleles it
  actually covers.
- A control value: of all groups of that same size, what fraction also cover every allele — a way to tell a
  genuinely compact panel from one that is unremarkable.
- An outcome, `panel_condition_met` or `panel_condition_failed`, fixed before the analysis ever runs.

### Internal mechanism (with references)

NetMHCIIpan-4.0 is a neural network trained on measured peptide-HLA binding affinities and on peptides
physically recovered from HLA molecules by mass spectrometry [2]. For one peptide-allele pair it reports a
*percentile rank*: how strongly that pair binds compared to a large set of random peptides. A lower rank means
stronger predicted binding; this analysis treats rank ≤ 10 as "binds" — a conventional threshold in the
HLA-binding literature, not something tuned on this data.

Given which peptides bind which alleles, the analysis is a covering-set search: among all peptides that bind
at least one allele, find the fewest peptides whose combined binders span all four alleles. It tries every
group size from one peptide upward and stops at the first size that works (an exhaustive search, capped at 8
peptides so it always finishes quickly).

### Internal pipeline, briefly

1. Read the peptide list, the allele list, and every cached prediction for that peptide × allele grid.
2. Mark each (peptide, allele) pair as a binder if its percentile rank ≤ 10.
3. Search for the smallest peptide group covering every allele.
4. Compare it against same-size random groups, as a control.
5. Record the outcome, the group found, and the control value.

### Where it sits in the ALS question, super simply

The study proposes that ALS may involve an autoimmune attack on C9orf72. For that attack to happen in many
patients despite their different HLA alleles, a small set of C9orf72 fragments would need to be recognisable
across the whole four-allele panel the study typed. This analysis checks exactly that narrow, predicted-binding
question. A "yes" does not show that the attack happens, is harmful, or is common in real patients — only that
the molecular prerequisite is not predicted to be rare in this panel. A "no" would weigh against that part of
the mechanism.

---

## 2. `ics_cytokine_skew_v1`: CD4+ cytokine skew in ALS vs. healthy donors

### Definition

When a T cell recognises a target, it does not just attack — it also sends chemical signals called *cytokines*,
and which cytokines it sends says what kind of immune response it is mounting. Michaelis *et al.* [1] measured
four cytokines (IFN-gamma, IL-4, IL-10, IL-17) in C9orf72-reactive CD4+ T cells, by a lab technique called
intracellular cytokine staining (ICS), and reported that ALS donors' cells favour a different cytokine balance
than healthy donors'. This analysis recomputes that comparison from the data the study deposited.

### Input

- The deposited flow-cytometry files (FCS) and the FlowJo workspace files that hold the authors' own cell
  selections ("gates") and event counts, for four ICS experiment batches ("screens") [1].
- The study's published figure source data, to check this recomputation against the authors' own numbers.

### Output

- For each of 15 ALS and 15 healthy donors: the antigen-specific response for each of the four cytokines.
- A single summary ratio per donor, IFN-gamma / (IL-4 + IL-10), and whether it differs between the two groups.
- How many donors' recomputed values exactly match the study's own published values — a direct check of
  whether this reanalysis reproduces its source, reported honestly even when it does not fully succeed.
- An outcome, `skew_replicated` or `skew_not_replicated`, with the statistical test fixed in advance.

### Internal mechanism (with references)

ICS is a standard immunology technique: cells are stimulated, then chemically "frozen" so that cytokines made
inside them stay trapped and can be stained and counted by flow cytometry [3]. A "gate" is a selection rule
(for example, "cells positive for this cytokine's stain") that an analyst draws once, in software, and the
software remembers. This analysis reads the gates and event counts the study's authors already drew and
recorded in their FlowJo workspace files; it does not redraw them.

For each donor, the cytokine-positive fraction measured in the antigen-stimulated sample minus the same
fraction in the unstimulated control sample gives the response that is specific to the antigen, rather than
background. IFN-gamma and IL-17 are associated with one broad style of T-cell response ("Th1/Th17"); IL-4 and
IL-10 with another ("Th2/regulatory") [4]. The ratio IFN-gamma / (IL-4 + IL-10) is therefore a single number
summarising which style dominates; the study reports this ratio as lower in ALS. Group differences are tested
with the Mann-Whitney U test [5], a standard non-parametric test for comparing two independent groups that
does not assume the data follow any particular distribution — appropriate here because small immunology
cohorts rarely do.

### Internal pipeline, briefly

1. Read each donor's stimulated and unstimulated well from the FlowJo workspace: the number of CD4+ T cells
   and how many of those are positive for each cytokine.
2. Subtract unstimulated from stimulated, per cytokine, per donor.
3. Compute each donor's IFN-gamma / (IL-4 + IL-10) ratio.
4. Compare the ALS and healthy-donor ratios with the Mann-Whitney test.
5. Separately, compare the recomputed per-donor values against the study's published source data, and report
   how many donors match.

### Where it sits in the ALS question, super simply

If C9orf72-reactive T cells in ALS patients are skewed toward a particular signalling style, that is a clue
about what kind of immune damage, if any, they could be doing. This analysis is a direct check of that one
published claim, using the study's own underlying data rather than only its summary figures. Because the
recomputation reproduces some but not all of the published per-donor values, a "replicated" outcome here
should be read as "this computation, on this data, points the same way" — not as independent proof beyond
what the study already reported, and the limitation is stated in the result itself.

---

## References

[1] Michaelis, T. *et al.* Autoimmune response to C9orf72 protein in amyotrophic lateral sclerosis. *Nature*
**647**, 970–978 (2025). https://doi.org/10.1038/s41586-025-09588-6

[2] Reynisson, B., Alvarez, B., Paul, S., Peters, B. & Nielsen, M. NetMHCpan-4.1 and NetMHCIIpan-4.0: improved
predictions of MHC antigen presentation by concurrent motif deconvolution and integration of MS MHC eluted
ligand data. *Nucleic Acids Research* **48**, W449–W454 (2020). https://doi.org/10.1093/nar/gkaa379

[3] Lamoreaux, L., Roederer, M. & Koup, R. Intracellular cytokine optimization and standard operating
procedure. *Nature Protocols* **1**, 1507–1516 (2006). https://doi.org/10.1038/nprot.2006.268

[4] Mosmann, T. R. & Coffman, R. L. TH1 and TH2 cells: different patterns of lymphokine secretion lead to
different functional properties. *Annual Review of Immunology* **7**, 145–173 (1989).
https://doi.org/10.1146/annurev.iy.07.040189.001045

[5] Mann, H. B. & Whitney, D. R. On a test of whether one of two random variables is stochastically larger
than the other. *The Annals of Mathematical Statistics* **18**, 50–60 (1947).
https://doi.org/10.1214/aoms/1177730491
