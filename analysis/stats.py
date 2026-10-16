from fractions import Fraction
from math import comb


def binom_cdf(k, n, p):
    p = Fraction(p)
    return sum(Fraction(comb(n, i)) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def _binom_pmf(k, n, p):
    return Fraction(comb(n, k)) * p**k * (1 - p) ** (n - k)


def clopper_pearson(k, n, alpha=Fraction(1, 20)):
    """Exact two-sided interval; alpha is the total tail probability."""
    alpha = Fraction(alpha)
    if k == 0:
        low = Fraction(0)
    else:
        lo, hi = Fraction(0), Fraction(1)
        for _ in range(80):
            mid = (lo + hi) / 2
            if 1 - binom_cdf(k - 1, n, mid) > alpha / 2:
                lo = mid
            else:
                hi = mid
        low = (lo + hi) / 2
    if k == n:
        high = Fraction(1)
    else:
        lo, hi = Fraction(0), Fraction(1)
        for _ in range(80):
            mid = (lo + hi) / 2
            if binom_cdf(k, n, mid) > alpha / 2:
                hi = mid
            else:
                lo = mid
        high = (lo + hi) / 2
    return low, high


def fisher_exact_2x2(a, b, c, d):
    n = a + b + c + d
    row1, col1 = a + b, a + c
    lo, hi = max(0, row1 - (n - col1)), min(row1, col1)
    den = comb(n, row1)
    observed = Fraction(comb(col1, a) * comb(n - col1, row1 - a), den)
    return sum(
        Fraction(comb(col1, x) * comb(n - col1, row1 - x), den)
        for x in range(lo, hi + 1)
        if Fraction(comb(col1, x) * comb(n - col1, row1 - x), den) <= observed
    )


def holm(p_values):
    ordered = sorted(enumerate(p_values), key=lambda x: x[1])
    adjusted = [Fraction(0)] * len(p_values)
    running = Fraction(0)
    for rank, (index, p) in enumerate(ordered):
        running = max(running, min(Fraction(1), p * (len(p_values) - rank)))
        adjusted[index] = running
    return adjusted


def spearman(xs, ys):
    """Rank correlation, stdlib only, with ties given their average rank.

    Pearson on the ranks: for ties the shortcut formula with 6*sum(d^2) is wrong, and response frequencies in a
    22-donor cohort tie often. Returns `None` when either side has no variance, because a correlation with a
    constant is undefined rather than zero.
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    rx, ry = ranks(list(xs)), ranks(list(ys))
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / (sxx * syy) ** 0.5
