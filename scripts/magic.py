"""check0r3000 — Magic Find scoring core.

Textual-free leaf module: turns the market snapshot + analyzed detail records into a
single quality score per tariff (higher = better) so the Magic Find tab can rank the
whole market by *quality*, not by a single column.

PRICE IS DELIBERATELY EXCLUDED from the score. The user's mandate is "rather more
expensive with more features than cheap with fewer features" — so the monthly premium
is shown and used only as a final tiebreaker when two tariffs score identically, never
as a penalty.

Leaf in the import DAG: depends only on tui_data (for the SnapshotRow/DetailRecord
types) and coverage_taxonomy (to classify free-text leistungen into canonical
categories). No Textual import — importable under a plain interpreter and runnable as
`python3 magic.py --selftest`, mirroring tui_data.py / coverage_taxonomy.py.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path

import coverage_taxonomy as ctax
import tui_data
from _modules import MODULE_KEYS as _MODULE_KEYS

REPO_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_PATH = REPO_ROOT / "config" / "magic-weights.json"
NEEDS_PATH = REPO_ROOT / "config" / "needs-weights.json"

# The eight canonical Bausteine (from _modules, derived from the tariff schema). A
# fixed denominator (not len(record.modules)) so a record that drops a key still scores
# against the full market breadth, and an extra key can't inflate the ratio above the
# others'.
# Reuse the worst→best tier ranking that tui_format pins; kept local so magic.py has
# no rendering dependency. Casefolded keys, matching _level_direction's lookup.
_LEVEL_RANK = {"basis": 0, "komfort": 1, "premium": 2}
_MAX_TIER = max(_LEVEL_RANK.values())  # 2 — best per-module tier


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------


@dataclass
class MagicWeights:
    """Per-dimension weights for the combined quality score.

    Defaults live here (so the tool works with no config file); override any subset
    from config/magic-weights.json. Every dimension is normalized to [0,1] before
    weighting, so the weights need not sum to 1 — but the defaults do, which keeps the
    total a clean [0,1] quality fraction. The user-confirmed balance: Tarifnote leads,
    Leistungs-coverage and Baustein-breadth are the second pillars, customer rating only
    nudges.

    `module_tier` was removed from the weighted score: in the real corpus only ~3/26
    records carry a per-module level (we deliberately pin level:null to stop haiku
    hallucinating tiers from the tariff name), so a tier weight rewarded extraction
    completeness, not quality. The tier ratio is still computed and surfaced as a
    detail-band info value (MagicScore.module_tier_raw), just not scored.
    """

    note: float = 0.35           # expert Tarifnote (the real market discriminator)
    leistung_cov: float = 0.22   # breadth of distinct benefit categories covered
    module_breadth: float = 0.20 # how many of the 8 Bausteine are included
    coverage_gen: float = 0.18   # generosity: sum insured / wait time / scope
    bewertung: float = 0.05      # CHECK24 customer rating (light nudge — clusters 3.8–4.2)

    # Candidate-pool size for the deep-scan funnel: how many top-prescored tariffs to
    # auto-harvest+analyze. Not a scoring weight; carried here so one config file holds
    # every Magic knob.
    pool_k: int = 25

    def dim_weights(self) -> dict[str, float]:
        """Just the five scoring weights, keyed by dimension name (drops pool_k)."""
        return {
            "note": self.note,
            "leistung_cov": self.leistung_cov,
            "module_breadth": self.module_breadth,
            "coverage_gen": self.coverage_gen,
            "bewertung": self.bewertung,
        }


def load_weights(path: Path | None = None) -> MagicWeights:
    """Load weights from config/magic-weights.json, falling back to code defaults.

    Unknown keys are ignored and any missing key keeps its default, so a partial file
    (override just `note`) is valid and a future field can't break an old config.
    """
    p = path or WEIGHTS_PATH
    w = MagicWeights()
    if not p.is_file():
        return w
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return w
    if not isinstance(data, dict):
        return w
    valid = w.dim_weights().keys()
    kwargs = {}
    for k in (*valid, "pool_k"):
        if k in data and isinstance(data[k], (int, float)) and not isinstance(data[k], bool):
            kwargs[k] = data[k]
    return replace(w, **kwargs)


def load_needs(path: Path | None = None) -> dict[str, float]:
    """Load the personal Bedarf weighting from config/needs-weights.json.

    Returns one weight per canonical Baustein; a missing file, unreadable JSON or a
    missing/invalid key falls back to the neutral 1.0 (so a partial file is valid and an
    all-neutral file ranks identically to the objective view). Negative weights are
    clamped to 0 at use-site (_module_stats); here we just reject non-numbers."""
    base = {k: 1.0 for k in _MODULE_KEYS}
    p = path or NEEDS_PATH
    if not p.is_file():
        return base
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return base
    if not isinstance(data, dict):
        return base
    for k in _MODULE_KEYS:
        v = data.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            base[k] = float(v)
    return base


def save_needs(weights: dict[str, float], path: Path | None = None) -> None:
    """Persist the personal Bedarf weighting to config/needs-weights.json, preserving the
    explanatory _comment and any unrelated keys already in the file. Only the eight
    canonical Baustein keys are written/updated, as plain numbers."""
    p = path or NEEDS_PATH
    data: dict = {}
    if p.is_file():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                data = existing
        except (json.JSONDecodeError, OSError):
            pass
    for k in _MODULE_KEYS:
        if k in weights:
            v = weights[k]
            # store ints cleanly (1 not 1.0) when the value is whole
            data[k] = int(v) if float(v).is_integer() else float(v)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def needs_are_neutral(needs: dict[str, float], eps: float = 1e-9) -> bool:
    """True when every Baustein weight is equal (so the Bedarf view == objective view).
    Lets the UI tell the user 'edit needs-weights.json to make this do something'."""
    vals = [max(0.0, needs.get(k, 1.0)) for k in _MODULE_KEYS]
    return max(vals) - min(vals) < eps


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


@dataclass
class MagicScore:
    """One ranked tariff: total quality + per-dimension breakdown for the detail band."""

    stem: str
    insurer: str
    product: str
    total: float                          # weighted quality fraction, [0,1]
    dims: dict[str, float] = field(default_factory=dict)     # raw normalized per-dim
    contrib: dict[str, float] = field(default_factory=dict)  # weighted, sums to total
    note: float | None = None             # parsed Tarifnote (lower = better)
    bewertung: float | None = None        # customer rating
    monatlich_eur: float | None = None    # representative price — display/tiebreak only
    n_modules: int = 0                    # included Bausteine (for the table)
    n_leistung_cats: int = 0              # distinct benefit categories covered
    module_tier_raw: float = 0.0          # tier ratio [0,1] — info only, NOT scored
    leistung_low_confidence: bool = False # benefit-recall looks thin (see rank())

    def quality_per_eur(self) -> float | None:
        """Quality units per euro/month — display-only efficiency, never a score input.
        None when no representative price is known or the price is non-positive."""
        if self.monatlich_eur is None or self.monatlich_eur <= 0:
            return None
        return self.total / self.monatlich_eur


@dataclass
class PreScore:
    """Snapshot-only pre-score for the deep-scan funnel (computable for all 214)."""

    insurer: str
    product: str
    stem: str | None
    note: float | None
    bewertung: float | None
    score: float
    has_detail: bool
    position: int


# ---------------------------------------------------------------------------
# Normalization helpers (shared by prescore + rank)
# ---------------------------------------------------------------------------


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def parse_note(s: str | float | None) -> float | None:
    """Parse a German Tarifnote ('1,0' / '2,3' / 1.0) to a float, or None.

    The snapshot stores the note as the DOM string with a decimal comma; a malformed
    or empty value yields None so the caller can neutralize that dimension rather than
    crash."""
    if s is None:
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s)
    txt = str(s).strip().replace(",", ".")
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _norm_note(note: float | None) -> float:
    """Tarifnote → [0,1], higher = better. 1,0 → 1.0, 2,5 → 0.0 (German school grades:
    smaller is better, and below ~2,5 a Rechtsschutz tariff is effectively unranked)."""
    if note is None:
        return 0.0
    return _clamp((2.5 - note) / (2.5 - 1.0))


def _norm_bewertung(v: float | None) -> float:
    """Customer rating → [0,1]. 3.5 → 0.0, 4.5 → 1.0. The real snapshot clusters
    3.8–4.2, so this lands ~0.3–0.7 — a deliberately light touch (weight 0.10). A
    missing rating is neutral (0.5), not a penalty."""
    if v is None:
        return 0.5
    return _clamp((v - 3.5) / (4.5 - 3.5))


def _module_stats(
    rec: tui_data.DetailRecord, needs: dict[str, float] | None = None
) -> tuple[int, float, float]:
    """(included_count, breadth[0,1], tier[0,1]) over the eight canonical Bausteine.

    With `needs` (the personal Bedarf weighting, one weight per Baustein) the breadth
    becomes a need-weighted match ratio — sum of weights for included Bausteine over the
    sum of all weights — so a tariff scores on how well it covers what the user actually
    cares about, not raw count. Neutral weights (all equal) reproduce the objective
    breadth exactly. tier stays objective (it is no longer scored anyway). `needs=None`
    is the objective default."""
    included = 0
    tier_sum = 0
    inc_keys: list[str] = []
    for key in _MODULE_KEYS:
        mod = rec.modules.get(key)
        if isinstance(mod, dict) and mod.get("included"):
            included += 1
            inc_keys.append(key)
            rank = _LEVEL_RANK.get(str(mod.get("level") or "").strip().casefold())
            if rank is not None:
                tier_sum += rank
    if needs:
        total_w = sum(max(0.0, needs.get(k, 1.0)) for k in _MODULE_KEYS)
        got_w = sum(max(0.0, needs.get(k, 1.0)) for k in inc_keys)
        breadth = (got_w / total_w) if total_w > 0 else 0.0
    else:
        breadth = included / len(_MODULE_KEYS)
    tier = tier_sum / (_MAX_TIER * len(_MODULE_KEYS))
    return included, breadth, tier


def _leistung_coverage(rec: tui_data.DetailRecord, tax: dict) -> tuple[int, float]:
    """(distinct_categories, coverage[0,1]). Counts DISTINCT canonical benefit
    categories, not raw leistungen — a verbose AVB listing the same benefit five ways
    must not outscore a terse one. Unmatched items (Sonstige) don't count."""
    keys = set()
    for item in rec.leistungen:
        k = ctax.classify(item, "leistung", tax)
        if k:
            keys.add(k)
    total_cats = len(ctax.ordered_keys("leistung", tax)) or 1
    return len(keys), _clamp(len(keys) / total_cats)


def _coverage_generosity(rec: tui_data.DetailRecord) -> float:
    """Heuristic [0,1] over three coverage fields, averaged.

    Each sub-score is deliberately coarse (the source is free text): sum insured
    (unlimited beats a finite cap beats unknown), waiting time (none beats short beats
    long), territorial scope (worldwide beats Europe beats unknown). Unknown maps to a
    neutral 0.5 so a sparse record isn't punished as if it were a *bad* value."""
    cov = rec.coverage if isinstance(rec.coverage, dict) else {}

    vs = str(cov.get("versicherungssumme") or "").lower()
    if not vs:
        vs_score = 0.5
    elif "unbegrenzt" in vs or "unlimit" in vs:
        vs_score = 1.0
    else:
        vs_score = 0.5  # a finite cap is present and quantified

    wt = cov.get("wartezeit_monate")
    if isinstance(wt, bool) or not isinstance(wt, (int, float)):
        wt_score = 0.5  # unknown / non-numeric
    elif wt <= 0:
        wt_score = 1.0
    elif wt <= 3:
        wt_score = 0.5
    else:
        wt_score = 0.0

    geo = str(cov.get("geltungsbereich") or "").lower()
    if not geo:
        geo_score = 0.5
    elif "weltweit" in geo or "world" in geo:
        geo_score = 1.0
    elif "europa" in geo or "europe" in geo:
        geo_score = 0.6
    else:
        geo_score = 0.3

    return (vs_score + wt_score + geo_score) / 3.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_one(
    stem: str,
    rec: tui_data.DetailRecord,
    note: float | None,
    bewertung: float | None,
    monatlich_eur: float | None,
    weights: MagicWeights,
    tax: dict | None = None,
    needs: dict[str, float] | None = None,
) -> MagicScore:
    """Compute the full MagicScore for one analyzed tariff. `needs` (optional) switches
    module_breadth to the personal Bedarf weighting; None = objective default."""
    tax = tax or ctax.load_taxonomy()
    n_mods, breadth, tier = _module_stats(rec, needs)
    n_cats, leistung_cov = _leistung_coverage(rec, tax)

    dims = {
        "note": _norm_note(note),
        "leistung_cov": leistung_cov,
        "module_breadth": breadth,
        "coverage_gen": _coverage_generosity(rec),
        "bewertung": _norm_bewertung(bewertung),
    }
    w = weights.dim_weights()
    contrib = {k: dims[k] * w[k] for k in dims}
    total = sum(contrib.values())
    return MagicScore(
        stem=stem,
        insurer=rec.insurer,
        product=rec.tariff,
        total=total,
        dims=dims,
        contrib=contrib,
        note=note,
        bewertung=bewertung,
        monatlich_eur=monatlich_eur,
        n_modules=n_mods,
        n_leistung_cats=n_cats,
        module_tier_raw=tier,
    )


def _representative_rows(rows: list[tui_data.SnapshotRow]) -> dict[str, tui_data.SnapshotRow]:
    """One snapshot row per stem (a tariff appears once per SB band). Picks the
    cheapest priced variant (tiebreak: lowest position) purely so the displayed
    price/rating is the headline one — none of the scored dimensions vary by SB band."""
    by_stem: dict[str, list[tui_data.SnapshotRow]] = {}
    for r in rows:
        if r.stem:
            by_stem.setdefault(r.stem, []).append(r)
    out: dict[str, tui_data.SnapshotRow] = {}
    for stem, variants in by_stem.items():
        priced = [r for r in variants if r.monatlich_eur is not None]
        pool = priced or variants
        out[stem] = min(
            pool,
            key=lambda r: (
                r.monatlich_eur if r.monatlich_eur is not None else float("inf"),
                r.position,
            ),
        )
    return out


def _flag_low_confidence(scores: list[MagicScore]) -> None:
    """Mark records whose distinct-benefit count looks like an extraction recall miss,
    not a genuinely thin tariff.

    `leistung_cov` rides on free-text extraction recall — cheap models omit benefits
    run-to-run — so a record far below the market's typical breadth gets a confidence
    flag, and the tab can warn instead of presenting a falsely precise score. The score
    itself is left untouched (we don't guess the missing benefits); only the display is
    qualified. Threshold = max(absolute floor 3, 40% of the median count). Fewer than two
    records flags nothing — there's no market to compare against. Mutates in place."""
    cats = [s.n_leistung_cats for s in scores]
    if len(cats) < 2:
        return
    floor = max(3.0, statistics.median(cats) * 0.4)
    for s in scores:
        s.leistung_low_confidence = s.n_leistung_cats < floor


def rank(
    rows: list[tui_data.SnapshotRow],
    details_by_stem: dict[str, tui_data.DetailRecord],
    weights: MagicWeights | None = None,
    tax: dict | None = None,
    needs: dict[str, float] | None = None,
) -> list[MagicScore]:
    """Rank every analyzed tariff by combined quality, best first.

    Joins detail records to snapshot rows by stem (for note/bewertung/price). A detail
    with no snapshot row still ranks — its note/bewertung are simply absent (neutralized
    in scoring). Ties break by lower price (the only place price ever enters), then stem
    for a stable order. `needs` (optional) re-weights module_breadth to the personal
    Bedarf view; None = objective default.
    """
    weights = weights or load_weights()
    tax = tax or ctax.load_taxonomy()
    reps = _representative_rows(rows)

    scores: list[MagicScore] = []
    for stem, rec in details_by_stem.items():
        row = reps.get(stem)
        note = parse_note(row.tarifnote) if row else None
        bew = row.bewertung if row else None
        price = row.monatlich_eur if row else None
        scores.append(score_one(stem, rec, note, bew, price, weights, tax, needs))

    _flag_low_confidence(scores)

    scores.sort(
        key=lambda s: (
            -s.total,
            s.monatlich_eur if s.monatlich_eur is not None else float("inf"),
            s.stem,
        )
    )
    return scores


def prescore(rows: list[tui_data.SnapshotRow]) -> list[PreScore]:
    """Snapshot-only pre-score over the whole market, best first.

    Cheap (no detail records, no model): `0.7*note + 0.3*bewertung`, both normalized.
    Drives the deep-scan funnel's candidate selection — pick the top-K, harvest+analyze
    the ones still missing a detail record. Deduped to one entry per (insurer, product),
    keeping the best-scoring variant.
    """
    best: dict[tuple[str, str], PreScore] = {}
    for r in rows:
        note = parse_note(r.tarifnote)
        score = 0.7 * _norm_note(note) + 0.3 * _norm_bewertung(r.bewertung)
        key = (r.insurer.strip(), r.product.strip())
        cur = best.get(key)
        if cur is None or score > cur.score:
            best[key] = PreScore(
                insurer=r.insurer,
                product=r.product,
                stem=r.stem,
                note=note,
                bewertung=r.bewertung,
                score=score,
                has_detail=r.has_detail,
                position=r.position,
            )
    out = list(best.values())
    out.sort(key=lambda p: (-p.score, p.position))
    return out


def select_candidates(
    pre: list[PreScore], k: int
) -> tuple[list[PreScore], list[PreScore]]:
    """Pick the top-K prescored products for the deep-scan, plus the products dropped
    at the K boundary that *tie* the cutoff score.

    `pre` must already be best-first (prescore() returns it so). The dropped-ties list
    lets the caller surface "N more share the cutoff score but didn't fit pool_k" — the
    CLAUDE.md "no silent caps" rule: a hard top-K must announce what it cut at a tie, so
    the user can raise pool_k instead of silently missing an equally-good tariff. A
    non-positive or oversized k means "take everything" (no cut, no ties dropped).
    """
    if k <= 0 or k >= len(pre):
        return list(pre), []
    selected = pre[:k]
    cutoff = selected[-1].score
    dropped_ties = [p for p in pre[k:] if abs(p.score - cutoff) < 1e-9]
    return selected, dropped_ties


# ---------------------------------------------------------------------------
# Self-test (Textual-free; mirrors tui_data.run_selftest)
# ---------------------------------------------------------------------------


def _mk_row(insurer, product, note, bew=None, price=None, pos=0, stem=None):
    return tui_data.SnapshotRow(
        position=pos, insurer=insurer, product=product, tarifnote=note,
        monatlich_eur=price, selbstbeteiligung="150", key=f"{insurer}-{pos}",
        bewertung=bew, stem=stem or f"{insurer.lower()}__{product.lower()}",
    )


def _mk_detail(insurer, product, n_modules=8, levels=None, leistungen=None,
               coverage=None):
    mods = {}
    for i, k in enumerate(_MODULE_KEYS):
        inc = i < n_modules
        lvl = (levels or {}).get(k)
        mods[k] = {"included": inc, "level": lvl}
    return tui_data.DetailRecord(
        insurer=insurer, tariff=product, stand="2026",
        modules=mods,
        coverage=coverage or {},
        leistungen=leistungen or [],
        ausschluesse=[], besonderheiten=[], beitrag=None,
    )


def _approx(a: float, b: float, eps: float = 1e-6) -> bool:
    return abs(a - b) <= eps


def _selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str):
        if not cond:
            failures.append(msg)

    w = MagicWeights()

    # 1. Default weights sum to 1.0 (clean [0,1] total).
    check(_approx(sum(w.dim_weights().values()), 1.0),
          f"default dim weights sum to {sum(w.dim_weights().values())}, expected 1.0")

    # 2. Note normalization anchors.
    check(_approx(_norm_note(1.0), 1.0), "note 1,0 should normalize to 1.0")
    check(_approx(_norm_note(2.5), 0.0), "note 2,5 should normalize to 0.0")
    check(_approx(_norm_note(1.75), 0.5), "note 1,75 should normalize to 0.5")
    check(_norm_note(3.0) == 0.0, "note 3,0 should clamp to 0.0")
    check(_norm_note(None) == 0.0, "missing note should be 0.0")

    # 3. Bewertung normalization (incl. neutral default).
    check(_approx(_norm_bewertung(4.5), 1.0), "bewertung 4.5 -> 1.0")
    check(_approx(_norm_bewertung(3.5), 0.0), "bewertung 3.5 -> 0.0")
    check(_approx(_norm_bewertung(None), 0.5), "missing bewertung -> neutral 0.5")

    # 4. parse_note handles comma, float, junk.
    check(_approx(parse_note("1,0"), 1.0), "parse_note('1,0') -> 1.0")
    check(_approx(parse_note(2.3), 2.3), "parse_note(2.3) -> 2.3")
    check(parse_note("") is None and parse_note("n/a") is None,
          "parse_note of empty/junk -> None")

    # 5. Module breadth + tier.
    full = _mk_detail("X", "full", n_modules=8)
    half = _mk_detail("X", "half", n_modules=4)
    _, br_full, _ = _module_stats(full)
    _, br_half, _ = _module_stats(half)
    check(_approx(br_full, 1.0), f"8/8 modules breadth should be 1.0, got {br_full}")
    check(_approx(br_half, 0.5), f"4/8 modules breadth should be 0.5, got {br_half}")
    tiered = _mk_detail("X", "tiered", n_modules=8,
                        levels={k: "Premium" for k in _MODULE_KEYS})
    _, _, tier_full = _module_stats(tiered)
    check(_approx(tier_full, 1.0), f"all-Premium tier should be 1.0, got {tier_full}")
    _, _, tier_none = _module_stats(full)
    check(_approx(tier_none, 0.0), f"level=None tier should be 0.0, got {tier_none}")

    # 6. Coverage generosity anchors.
    gen_best = _coverage_generosity(_mk_detail("X", "g", coverage={
        "versicherungssumme": "unbegrenzt", "wartezeit_monate": 0,
        "geltungsbereich": "weltweit"}))
    check(_approx(gen_best, 1.0), f"best coverage generosity should be 1.0, got {gen_best}")
    gen_unknown = _coverage_generosity(_mk_detail("X", "g", coverage={}))
    check(_approx(gen_unknown, 0.5), f"empty coverage should be neutral 0.5, got {gen_unknown}")

    # 7. Distinct-category coverage (dedup verbose listings).
    tax = ctax.load_taxonomy()
    verbose = _mk_detail("X", "v", leistungen=[
        "telefonische Rechtsberatung",
        "telefonische Rechtsberatung (ARAG JuraTel®)",
        "telefonische Rechtsberatung (DMB-Hotline)",
    ])
    n_cats, cov = _leistung_coverage(verbose, tax)
    check(n_cats == 1, f"three phrasings of one benefit should be 1 category, got {n_cats}")

    # 8. End-to-end: a strictly-better tariff outranks a worse one.
    rows = [
        _mk_row("Alpha", "Top", "1,0", bew=4.2, price=40.0, pos=1),
        _mk_row("Beta", "Weak", "2,4", bew=3.8, price=20.0, pos=2),
    ]
    good = _mk_detail("Alpha", "Top", n_modules=8, leistungen=[
        "telefonische Rechtsberatung", "Mediation", "Strafkaution als Darlehen",
        "Mobiler Anwalt (Hausbesuch)", "freie Anwaltswahl",
    ], coverage={"versicherungssumme": "unbegrenzt", "wartezeit_monate": 0,
                 "geltungsbereich": "weltweit"})
    weak = _mk_detail("Beta", "Weak", n_modules=2, leistungen=[],
                      coverage={"wartezeit_monate": 6, "geltungsbereich": "Deutschland"})
    details = {"alpha__top": good, "beta__weak": weak}
    ranked = rank(rows, details, w, tax)
    check(len(ranked) == 2, f"expected 2 ranked, got {len(ranked)}")
    check(ranked[0].stem == "alpha__top",
          f"better tariff should rank first, got {ranked[0].stem}")
    check(0.0 <= ranked[0].total <= 1.0, f"total out of [0,1]: {ranked[0].total}")
    check(_approx(ranked[0].total, sum(ranked[0].contrib.values())),
          "total must equal sum of contributions")

    # 9. Price is NOT a scoring factor: same record, wildly different price -> same total.
    rows_cheap = [_mk_row("Alpha", "Top", "1,0", bew=4.2, price=10.0)]
    rows_dear = [_mk_row("Alpha", "Top", "1,0", bew=4.2, price=999.0)]
    t_cheap = rank(rows_cheap, {"alpha__top": good}, w, tax)[0].total
    t_dear = rank(rows_dear, {"alpha__top": good}, w, tax)[0].total
    check(_approx(t_cheap, t_dear), "price must not change the quality total")

    # 10. prescore: dedup per product, order by score.
    pre = prescore(rows)
    check(len(pre) == 2, f"prescore should dedup to 2 products, got {len(pre)}")
    check(pre[0].insurer == "Alpha", f"prescore top should be Alpha, got {pre[0].insurer}")

    # 11. Candidate selection: top-K + boundary ties announced (no silent cap).
    tie_rows = [
        _mk_row("A", "p1", "1,0", bew=4.1, pos=1),  # all four tie on note+bew -> same score
        _mk_row("B", "p2", "1,0", bew=4.1, pos=2),
        _mk_row("C", "p3", "1,0", bew=4.1, pos=3),
        _mk_row("D", "p4", "2,0", bew=4.1, pos=4),  # lower note -> strictly below the tie
    ]
    tie_pre = prescore(tie_rows)
    sel, dropped = select_candidates(tie_pre, 2)
    check(len(sel) == 2, f"select_candidates k=2 should pick 2, got {len(sel)}")
    check(len(dropped) == 1,
          f"one product ties the cutoff past the cap, got {len(dropped)} dropped")
    check(dropped[0].product == "p3",
          f"the dropped tie should be the 3rd-by-position p3, got {dropped[0].product}")
    sel_all, dropped_all = select_candidates(tie_pre, 99)
    check(len(sel_all) == 4 and not dropped_all,
          "k >= len should take all with no dropped ties")
    sel_lower, dropped_lower = select_candidates(tie_pre, 3)
    check(len(sel_lower) == 3 and not dropped_lower,
          "cutoff above a strictly-lower next item drops no ties")

    # 12. module_tier is no longer a scored dimension — info only.
    check("module_tier" not in ranked[0].dims,
          "module_tier must not appear in scored dims")
    check("module_tier" not in ranked[0].contrib,
          "module_tier must not contribute to the total")
    tiered_score = score_one("x__t", tiered, 1.0, 4.0, 50.0, w, tax)
    check(_approx(tiered_score.module_tier_raw, 1.0),
          f"all-Premium tier should surface as module_tier_raw=1.0, "
          f"got {tiered_score.module_tier_raw}")
    check("module_tier" not in tiered_score.contrib,
          "module_tier_raw must not leak into contrib")

    # 13. Low-confidence flag: thin benefit recall is flagged, broad is not.
    lc_rows = [
        _mk_row("R", "rich", "1,0", bew=4.1, price=50.0, pos=1, stem="r__rich"),
        _mk_row("P", "poor", "1,0", bew=4.1, price=50.0, pos=2, stem="p__poor"),
    ]
    rich = _mk_detail("R", "rich", n_modules=8, leistungen=[
        "telefonische Rechtsberatung", "Mediation", "Strafkaution als Darlehen",
        "Mobiler Anwalt (Hausbesuch)", "freie Anwaltswahl", "Ehe- und Familienrecht"])
    poor = _mk_detail("P", "poor", n_modules=8, leistungen=[])
    lc = rank(lc_rows, {"r__rich": rich, "p__poor": poor}, w, tax)
    by_stem = {s.stem: s for s in lc}
    check(by_stem["p__poor"].leistung_low_confidence,
          "an empty-benefit record should be flagged low-confidence")
    check(not by_stem["r__rich"].leistung_low_confidence,
          f"a broad record ({by_stem['r__rich'].n_leistung_cats} cats) should not be "
          f"flagged low-confidence")

    # 14. quality_per_eur: display-only efficiency; None when price absent / zero.
    qpe = by_stem["r__rich"]
    check(qpe.quality_per_eur() is not None
          and _approx(qpe.quality_per_eur(), qpe.total / 50.0),
          "quality_per_eur should be total/price")
    free = score_one("x__f", rich, 1.0, 4.1, None, w, tax)
    check(free.quality_per_eur() is None, "quality_per_eur is None without a price")
    zero = score_one("x__z", rich, 1.0, 4.1, 0.0, w, tax)
    check(zero.quality_per_eur() is None, "quality_per_eur is None at price 0")

    # 15. Need-weighting: neutral == objective, skewed shifts module_breadth.
    neutral = {k: 1.0 for k in _MODULE_KEYS}
    check(needs_are_neutral(neutral), "all-equal needs should read as neutral")
    skewed = {k: 0.0 for k in _MODULE_KEYS}
    skewed["privat"] = 1.0  # only privat matters
    check(not needs_are_neutral(skewed), "skewed needs should not read as neutral")
    rec_priv = _mk_detail("X", "p", n_modules=0)
    rec_priv.modules["privat"] = {"included": True, "level": None}
    rec_priv.modules["verkehr"] = {"included": True, "level": None}
    _, br_obj, _ = _module_stats(rec_priv)            # 2/8 objective
    check(_approx(br_obj, 2 / 8), f"objective breadth should be 2/8, got {br_obj}")
    _, br_need, _ = _module_stats(rec_priv, skewed)   # only privat weighted, privat covered
    check(_approx(br_need, 1.0),
          f"need-weighted breadth (only privat matters, privat covered) should be 1.0, "
          f"got {br_need}")
    _, br_neutral, _ = _module_stats(rec_priv, neutral)
    check(_approx(br_neutral, br_obj), "neutral needs must reproduce objective breadth")
    nd = load_needs()
    check(set(nd.keys()) == set(_MODULE_KEYS), "load_needs returns all 8 Baustein keys")
    # save_needs roundtrip (to a temp file; never touches the real config)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "needs.json"
        want = {k: 1.0 for k in _MODULE_KEYS}
        want["privat"] = 3.0
        want["verkehr"] = 0.0
        save_needs(want, tmp)
        got = load_needs(tmp)
        check(got == want, f"save/load_needs roundtrip mismatch: {got}")
        # whole numbers persist as ints (1 not 1.0) but load back as floats
        raw = json.loads(tmp.read_text(encoding="utf-8"))
        check(raw["privat"] == 3 and isinstance(raw["privat"], int),
              f"whole weights should persist as int, got {raw['privat']!r}")

    # 16. Real-data smoke: rank loads and scores every record without raising.
    real_rows: list[tui_data.SnapshotRow] = []
    snap_dir = REPO_ROOT / "data" / "snapshots"
    latest = tui_data._find_latest_snapshot(snap_dir)
    if latest is not None:
        snap = tui_data.load_snapshot(latest)
        if snap is not None:
            real_rows = snap.rows
    real_details = dict(tui_data.load_all_details())
    real_ranked = rank(real_rows, real_details, w, tax)
    check(len(real_ranked) == len(real_details),
          f"every detail should score: {len(real_ranked)} vs {len(real_details)}")
    for s in real_ranked:
        check(0.0 <= s.total <= 1.0, f"{s.stem} total out of range: {s.total}")

    # ---- report ----
    print(f"=== magic.py selftest ===")
    print(f"checks run, {len(failures)} failure(s)")
    for f in failures:
        print(f"  FAIL: {f}")

    if real_ranked:
        print(f"\nTop {min(10, len(real_ranked))} by quality "
              f"(of {len(real_ranked)} analyzed):")
        for i, s in enumerate(real_ranked[:10], 1):
            note = f"{s.note:.1f}" if s.note is not None else "—"
            bew = f"{s.bewertung:.1f}" if s.bewertung is not None else "—"
            price = f"{s.monatlich_eur:.0f}€" if s.monatlich_eur is not None else "—"
            print(f"  {i:>2}. {s.total:.3f}  {s.stem:<48} "
                  f"note {note}  bew {bew}  mod {s.n_modules}/8  "
                  f"leist {s.n_leistung_cats}/24  {price}")

    if real_rows:
        pre_real = prescore(real_rows)
        print(f"\nPre-score over {len(real_rows)} snapshot rows "
              f"-> {len(pre_real)} distinct products; top 5:")
        for i, p in enumerate(pre_real[:5], 1):
            note = f"{p.note:.1f}" if p.note is not None else "—"
            flag = "✓detail" if p.has_detail else "·missing"
            print(f"  {i}. {p.score:.3f}  {p.insurer} / {p.product}  "
                  f"note {note}  {flag}")

    if failures:
        print("\nMAGIC SELFTEST FAILED")
        return 1
    print("\nMAGIC SELFTEST OK")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Magic Find scoring core + self-test.")
    ap.add_argument("--selftest", action="store_true",
                    help="assert scoring invariants and rank the real records")
    ap.parse_args()
    raise SystemExit(_selftest())
