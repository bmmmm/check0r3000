"""check0r3000 — pure text-formatting helpers.

Textual-free leaf module: every function here returns plain strings (some carry
Textual markup tags as literal text). Row/record arguments are duck-typed, so
this module imports no data model and pulls in no Textual."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from _modules import MODULE_LABELS  # stdlib-only leaf: canonical Baustein keys/labels

if TYPE_CHECKING:  # annotations only; never imported at runtime (keeps this leaf
    from tui_data import DetailRecord, SnapshotRow  # Textual-free, data-model-free)


# Data-availability status, from "we only read the listing" to "fully analyzed".
# The glyphs are explained by STATUS_LEGEND, shown above the Market table.
def _status_glyph(row: SnapshotRow) -> str:
    if row.has_detail:
        return "[bright_green]✓[/bright_green]"
    if row.has_pdf:
        return "[cyan]↓[/cyan]"
    if row.has_urls:
        return "[yellow]○[/yellow]"
    return "[dim]·[/dim]"


STATUS_LEGEND = (
    "[bold]Status[/bold]  [bright_green]✓[/bright_green] analysiert   "
    "[cyan]↓[/cyan] PDF lokal   [yellow]○[/yellow] URLs (\\[g] lädt + analysiert)   "
    "[dim]·[/dim] nur gelistet"
)


# Customer-rating colour anchors for the 4.x gradient (gold-yellow → vivid green).
# Stretched across the snapshot's OBSERVED >=4.0 range so neighbours (4.1 vs 4.2)
# differ clearly — a fixed scale paints the whole 3.8-4.2 cluster one colour.
_BEW_LOW = (0xE5, 0xC0, 0x00)
_BEW_HIGH = (0x3A, 0xE6, 0x6B)


def _lerp_hex(c0: tuple[int, int, int], c1: tuple[int, int, int], t: float) -> str:
    """Linear RGB interpolation between two colours → a #rrggbb string."""
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    r, g, b = (round(a + (z - a) * t) for a, z in zip(c0, c1))
    return f"#{r:02x}{g:02x}{b:02x}"


def _bewertung_color(v: float, lo: float | None = None, hi: float | None = None) -> str:
    """Colour for a CHECK24 customer rating (0-5). Ratings cluster tightly in the 4.x
    band (live: 3.8-4.2, ~80% at 4.1), so the >=4.0 band gets a fine data-relative
    gradient stretched across the observed [lo, hi]; below 4.0 a coarse two-band split
    is enough. lo/hi are the snapshot's bewertung min/max — falls back when unknown."""
    if v < 3.5:
        return "bright_red"
    if v < 4.0:
        return "#d78700"  # coarse amber for the sub-4.0 ratings (3.5-3.9)
    base = lo if (lo is not None and lo >= 4.0) else 4.0
    top = hi if (hi is not None and hi > base) else base + 0.5
    span = top - base
    t = 1.0 if span < 0.05 else (v - base) / span
    return _lerp_hex(_BEW_LOW, _BEW_HIGH, t)


def _bewertung_cell(row: SnapshotRow, lo: float | None = None,
                    hi: float | None = None) -> str:
    """CHECK24 customer rating (0-5 stars), distinct from the expert Tarifnote. Shows
    '—' until a scrape with rating support has populated the snapshot. lo/hi are the
    snapshot-wide rating range for the data-relative colour (see _bewertung_color)."""
    if row.bewertung is None:
        return "[dim]—[/dim]"
    v = row.bewertung
    color = _bewertung_color(v, lo, hi)
    val = f"{v:.1f}".replace(".", ",")
    cnt = f" [dim]({row.bewertung_anzahl})[/dim]" if row.bewertung_anzahl else ""
    return f"[{color}]{val}★[/{color}]{cnt}"


# Magic-Find quality-score colour anchors (muted red = low → vivid green = high).
# The score is an absolute [0,1] quality fraction, so a fixed scale is intended here:
# the bar should read "how good overall", not "best-in-this-batch".
_MAGIC_LOW = (0xD0, 0x4A, 0x4A)
_MAGIC_HIGH = (0x3A, 0xE6, 0x6B)


def magic_score_color(frac: float) -> str:
    """Colour for a Magic quality fraction [0,1]: red (low) → green (high)."""
    return _lerp_hex(_MAGIC_LOW, _MAGIC_HIGH, frac)


def magic_bar(frac: float, width: int = 8) -> str:
    """A colour-graded mini bar (filled █ / empty ░) for a [0,1] fraction."""
    frac = 0.0 if frac < 0.0 else 1.0 if frac > 1.0 else frac
    filled = round(frac * width)
    color = magic_score_color(frac)
    return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}]"


def magic_score_cell(total: float, width: int = 8) -> str:
    """Score number + colour-graded mini bar for the Magic Find table/detail."""
    return f"{total:.3f} {magic_bar(total, width)}"


# ---------------------------------------------------------------------------
# Coverage-comparison (Vergleich tab) rendering helpers — pure, no Textual.
# Cells are padded on PLAIN text then optionally wrapped in one colour tag, so
# trailing (invisible) coloured spaces keep columns aligned without markup-width
# math. Glyphs used (✓ ✗ ★ — €) are all terminal width 1.
# ---------------------------------------------------------------------------

VERGLEICH_LABEL_W = 30  # width of the left-hand row-label column


def _vergleich_col_w(ncols: int, avail: int = 130) -> int:
    """Per-tariff column width, shrinking as more tariffs are compared. `avail` is the
    usable render width — the label column plus all tariff columns must fit inside it,
    so the matrix rows never exceed the terminal and wrap."""
    if ncols <= 0:
        return 16
    return max(13, min(24, (avail - VERGLEICH_LABEL_W) // ncols))


def _esc(s) -> str:
    """Escape a literal '[' so data text can't be parsed as Textual markup. A lone
    ']' is harmless, so only '[' needs escaping. Tolerates non-string input (a model
    may emit a number/bool where text is expected) by stringifying it."""
    if s is None:
        return ""
    return str(s).replace("[", r"\[")


def link_url(url) -> str:
    """Sanitize a URL for use inside a ``[link="{url}"]`` markup attribute. A literal
    '"' would close the attribute early and a '[' / ']' would be parsed as Textual
    markup — a manifest doc URL that carries any of them would break the tag (or its
    surrounding cell). Percent-encode exactly those three (all valid URL escapes),
    leaving existing %-escapes untouched. Tolerates non-string input (stringified)."""
    if not url:
        return ""
    return (str(url).replace('"', "%22").replace("[", "%5B").replace("]", "%5D"))


def _pad_cell(plain: str, width: int, color: str | None = None) -> str:
    """Truncate/pad PLAIN text to `width`, then escape and wrap in one colour tag.

    Escaping happens AFTER the width math (a trailing '\\[' renders as one visible
    '[', so padding the unescaped string keeps columns aligned)."""
    if len(plain) > width:
        plain = plain[: max(1, width - 1)] + "…"
    cell = _esc(plain.ljust(width))
    return f"[{color}]{cell}[/{color}]" if color else cell


def _pad_label(plain: str) -> str:
    """Truncate+pad a row label to the fixed left column. Category names run up to
    ~49 chars; without truncation they overflow VERGLEICH_LABEL_W and shove every
    tariff cell out of alignment, which is exactly the misalignment seen in the
    matrices. Truncating here keeps the glyph columns flush."""
    return _pad_cell(plain, VERGLEICH_LABEL_W)


def _trunc(plain: str, width: int) -> str:
    """Escape + hard-truncate a free-text continuation line to `width` so it never
    wraps onto the next row (wrapping is what makes the verbose subtext unreadable)."""
    if len(plain) > width:
        plain = plain[: max(1, width - 1)] + "…"
    return _esc(plain)


def _col_label(stem: str) -> str:
    """Short column header from a stem's insurer part (arag -> ARAG, advocard ->
    Advocard)."""
    head = stem.split("__")[0]
    return head.upper() if len(head) <= 4 else head[:1].upper() + head[1:]


_MODULE_LEVEL_CELLS = {
    "premium": ("★★★ Premium", "bright_green"),
    "komfort": ("★★ Komfort", "yellow"),
    "basis": ("★ Basis", "white"),
}


def _module_cell(mod: dict[str, Any]) -> tuple[str, str]:
    """(plain, colour) for one module in a tariff column. Level lookup is
    casefolded, matching _level_direction/magic._module_stats -- a model
    emitting "premium"/"KOMFORT" must not silently fall through to the
    generic "✓" cell."""
    if not isinstance(mod, dict) or not mod.get("included"):
        return "—", "dim"
    level = mod.get("level")
    key = level.strip().casefold() if isinstance(level, str) else None
    return _MODULE_LEVEL_CELLS.get(key, ("✓", "cyan"))


# Module quality tiers, worst→best. Comparing level strings directly is wrong in
# general — it is only accidentally right for Basis/Komfort/Premium (B<K<P) and
# breaks for any other label or a different casing — so rank them explicitly.
_LEVEL_RANK = {"basis": 0, "komfort": 1, "premium": 2}


def _level_direction(old: str | None, new: str | None) -> int | None:
    """Compare two module tiers: +1 upgrade, -1 downgrade, 0 same rank, None when
    either tier is unknown (so the caller can stay neutral instead of guessing)."""
    o = _LEVEL_RANK.get((old or "").strip().casefold())
    n = _LEVEL_RANK.get((new or "").strip().casefold())
    if o is None or n is None:
        return None
    return (n > o) - (n < o)


def _fmt_eur(v) -> str:
    """A monthly/yearly EUR amount for the detail band. A model may emit a numeric
    string ("12,50") or other type where the schema wants a number; format real
    numbers as before and fall back to the escaped raw text instead of crashing on
    f'{v:.2f}'."""
    if not isinstance(v, bool) and isinstance(v, (int, float)):
        return f"{v:.2f}"
    return _esc(str(v))


def _amount_tokens(s: str | None) -> list[str]:
    """Digit-run tokens from a German-formatted amount string, in order. Treats '.',
    spaces, NBSP and narrow-NBSP as thousands grouping (dropped) and a ',<digits>'
    decimal fraction as cents (dropped), so distinct numbers stay distinct while one
    grouped number stays whole: '1 000 000,50' -> ['1000000'], '150 - 300' ->
    ['150', '300'], '150,00' -> ['150'] (not the garbage '150', '00')."""
    import re

    s = (s or "").replace("\u00a0", " ").replace("\u202f", " ")
    s = re.sub(r",\d+", "", s)
    return [re.sub(r"[.\s]", "", m) for m in re.findall(r"\d{1,3}(?:[.\s]\d{3})+|\d+", s)]


def _distinct_numbers(s: str, limit: int = 2) -> list[str]:
    out: list[str] = []
    for n in _amount_tokens(s):
        if n not in out:
            out.append(n)
        if len(out) >= limit:
            break
    return out


def _short_versicherungssumme(v: str | None) -> str:
    if not v:
        return "k.A."
    low = v.lower()
    # A finite per-variant cap must stay visible even when another variant is
    # unlimited: "2000000 EUR (Smart); unbegrenzt (Best)" is NOT blanket-unlimited
    # cover, and collapsing it to "unbegrenzt*" silently hid the Smart 2-Mio cap.
    # _amount_tokens handles NBSP/space-grouped thousands and decimal commas, so
    # "1 000 000" / "2.000.000,00" no longer misparse to "1/0" / "2 Mio./0".
    parts: list[str] = []
    for raw in _amount_tokens(v):
        n = int(raw)
        if n >= 1_000_000:
            token = f"{n / 1_000_000:g}".replace(".", ",") + " Mio."
        elif n >= 1_000:
            token = f"{n // 1000} Tsd."
        else:
            token = str(n)
        if token not in parts:
            parts.append(token)
    if "unbegrenzt" in low and "unbegr." not in parts:
        parts.append("unbegr.")
    if not parts:
        return v
    if parts == ["unbegr."]:
        # purely unlimited; keep the familiar wording, star = carries qualifiers
        return "unbegrenzt*" if len(v.strip()) > len("unbegrenzt") + 1 else "unbegrenzt"
    return "/".join(parts)


def _short_selbstbeteiligung(v: str | None) -> str:
    if not v:
        return "k.A."
    # "ohne" (zero excess) is a meaningful, distinct option; keep it visible
    # instead of letting the digit-only token parse silently drop it.
    parts = (["ohne"] if "ohne" in v.lower() else []) + _distinct_numbers(v, limit=2)
    return ("/".join(parts) + " €") if parts else v


def _short_geltungsbereich(v: str | None) -> str:
    if not v:
        return "k.A."
    low = v.lower()
    if "europa" in low:
        return "Europa+ww. temp." if ("weltweit" in low or "ww" in low) else "Europa"
    return v


def _short_vertragslaufzeit(v: str | None) -> str:
    if not v:
        return "k.A."
    low = v.lower()
    # match the abbreviation "tägl." too, not only the full word "täglich"
    tag = ", tägl." if ("tägl" in low or "taegl" in low) else ""
    # keep every distinct term option ("1, 2, 3, 4 oder 5 Jahre" -> "1/2/3/4/5
    # J."); do not truncate the list to the first two.
    nums = _distinct_numbers(v, limit=8)
    return ("/".join(nums) + " J." + tag) if nums else v


def _short_wartezeit(cov: dict[str, Any]) -> str:
    m = cov.get("wartezeit_monate")
    return f"{m} Mon." if m is not None else "k.A."


def _price_quartiles(rows: list[SnapshotRow]) -> tuple[float, float, float]:
    """Return (q1, median, q3) for monatlich_eur, ignoring None."""
    prices = sorted(r.monatlich_eur for r in rows if r.monatlich_eur is not None)
    if not prices:
        return (0, 0, 0)
    n = len(prices)
    q1 = prices[n // 4]
    median = prices[n // 2]
    q3 = prices[3 * n // 4]
    return q1, median, q3


def _score_colour(total: float, dnf: bool) -> str:
    """Score-band colour: green >=90, yellow >=60, red below / DNF. Used only for the
    SCORE column — every other column stays neutral so the eye lands on the verdict."""
    if dnf or total < 60:
        return "bright_red"
    return "bright_green" if total >= 90 else "yellow"


def benchmark_markup(meta: dict, groups: list) -> str:
    """Render scored benchmark groups as an aligned, colour-coded markup block for the
    TUI Benchmark tab. `meta` is the benchmarks/results.json header (generated/commit/
    repeat); `groups` is the [(tariff, [(row, points), ...]), ...] sequence from
    scorecard.scored_by_tariff. Duck-typed — dicts in, one markup string out, no data
    model and no Textual, like every helper here. Only the SCORE column is coloured by
    value; latency and cost are shown but, by design, never folded into the points."""
    if not groups:
        return ("[dim italic]Noch keine Benchmark-Daten.\n"
                "Ein Lauf mit [bold]--save-summary[/bold] (z. B. \\[g] in der TUI oder "
                "`check0r-bench … --save-summary`) schreibt benchmarks/results.json — "
                "dann erscheint hier die Scorecard.[/dim italic]")
    # Header padding mirrors the data row below exactly (two spaces before ~wall).
    head = (f"{'Modell':<28} {'Input':<13} {'Fth':>4} {'Sch':>4} {'Hal':>4} "
            f"{'Mod':>4} {'Score':>5}  {'~wall':>6} {'~Kosten':>8}")
    out = [
        f"[bold]Benchmark — Extraktionsqualität[/bold]   "
        f"[dim]{_esc(meta.get('generated', '?'))} · commit "
        f"{_esc(meta.get('commit', '?'))} · {_esc(meta.get('repeat', '?'))} "
        f"Läufe/Zelle[/dim]",
        "[dim]Punkte = reine Korrektheit (reproduzierbar): Faithful 50 · Schema 20 · "
        "Halluz-frei 15 · Module 15 = 100. Latenz/Kosten sind Betriebsdaten, nicht im "
        "Score.  [bright_green]●[/bright_green] ≥90  [yellow]●[/yellow] ≥60  "
        "[bright_red]●[/bright_red] <60.[/dim]",
        "",
    ]
    for tariff, scored in groups:
        out.append(f"[bold cyan]{_esc(tariff)}[/bold cyan]")
        out.append(f"[dim]{head}[/dim]")
        for r, s in scored:
            # Local backends (omlx:/mlx:/ollama:) report no cost; flag the model cyan.
            model_col = "cyan" if r.get("cost_usd") is None else None
            model_cell = _pad_cell(str(r.get("model", "?")), 28, model_col)
            input_cell = _pad_cell(str(r.get("input", "")), 13)
            dnf = bool(s.get("dnf"))
            score_txt = "DNF" if dnf else f"{s['total']:.0f}"
            col = _score_colour(s.get("total", 0.0), dnf)
            score_cell = f"[{col} bold]{score_txt:>5}[/{col} bold]"
            wall = f"{r['wall_s']:.0f}s" if r.get("wall_s") is not None else "–"
            cost = f"${r['cost_usd']:.2f}" if r.get("cost_usd") is not None else "–"
            out.append(
                f"{model_cell} {input_cell} "
                f"{s['faithful']:>4.0f} {s['schema']:>4.0f} {s['halluc']:>4.0f} "
                f"{s['modules']:>4.0f} {score_cell}  {wall:>6} {cost:>8}"
            )
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Detail-band / Verlauf row rendering — pure (self-free) formatters moved out of
# CheckApp so the detail markup lives with the other formatters, not the App.
# ---------------------------------------------------------------------------

def _module_badge(mod: dict[str, Any]) -> str:
    """Full coloured badge for one module in the detail band (vs _module_cell, which
    returns a padded (plain, colour) pair for the aligned Vergleich matrix)."""
    if not mod.get("included"):
        return "[dim]—[/dim]"
    level = mod.get("level")
    if level == "Premium":
        return "[bright_green]★★★ Premium[/bright_green]"
    if level == "Komfort":
        return "[yellow]★★ Komfort[/yellow]"
    if level == "Basis":
        return "[white]★ Basis[/white]"
    return "[cyan]✓[/cyan]"


def record_body_lines(detail: "DetailRecord") -> list[str]:
    """Modules → coverage → premium → benefits → exclusions → highlights, as markup
    lines. Shared by the Market detail band and the Favorites band (when a record
    exists). Duck-typed on the DetailRecord shape — no data-model import at runtime."""
    lines: list[str] = []

    # Modules
    lines.append("[bold underline]Module[/bold underline]")
    for mod_key, label in MODULE_LABELS.items():
        mod = detail.modules.get(mod_key, {})
        included = mod.get("included", False)
        badge_str = _module_badge(mod)
        note_str = mod.get("note") or ""
        lines.append(f"  {label:<22} {badge_str}")
        if included and note_str:
            lines.append(f"    [dim]{_esc(note_str)}[/dim]")
    lines.append("")

    # Coverage — every value is model-emitted free text, so escape '[' (a
    # literal bracket would otherwise be eaten by / break Rich markup).
    cov = detail.coverage
    if cov:
        lines.append("[bold underline]Deckung[/bold underline]")
        if cov.get("versicherungssumme"):
            lines.append(f"  Versicherungssumme:  {_esc(str(cov['versicherungssumme']))}")
        if cov.get("selbstbeteiligung"):
            lines.append(f"  Selbstbeteiligung:   {_esc(str(cov['selbstbeteiligung']))}")
        if cov.get("wartezeit_monate") is not None:
            lines.append(f"  Wartezeit:           {_esc(cov['wartezeit_monate'])} Monate")
        if cov.get("wartezeit_ausnahmen"):
            lines.append("  Wartezeit-Ausnahmen:")
            wa = cov["wartezeit_ausnahmen"]
            for ex in (wa if isinstance(wa, list) else [wa]):
                lines.append(f"    • {_esc(str(ex))}")
        if cov.get("geltungsbereich"):
            lines.append(f"  Geltungsbereich:     {_esc(str(cov['geltungsbereich']))}")
        if cov.get("vertragslaufzeit"):
            lines.append(f"  Vertragslaufzeit:    {_esc(str(cov['vertragslaufzeit']))}")
        lines.append("")

    # Premium
    if detail.beitrag:
        lines.append("[bold underline]Beitrag[/bold underline]")
        m = detail.beitrag.get("monatlich_eur")
        y = detail.beitrag.get("jaehrlich_eur")
        if m is not None:
            lines.append(f"  [bright_green]€ {_fmt_eur(m)} / Monat[/bright_green]")
        if y is not None:
            lines.append(f"  € {_fmt_eur(y)} / Jahr")
        if detail.beitrag.get("quelle"):
            lines.append(f"  Quelle: {_esc(str(detail.beitrag['quelle']))}")
        lines.append("")

    # Leistungen
    if detail.leistungen:
        lines.append("[bold underline]Leistungen[/bold underline]")
        for item in detail.leistungen:
            lines.append(f"  [green]✓[/green] {_esc(item)}")
        lines.append("")

    # Ausschlüsse
    if detail.ausschluesse:
        lines.append("[bold underline]Ausschlüsse[/bold underline]")
        for item in detail.ausschluesse:
            lines.append(f"  [red]✗[/red] {_esc(item)}")
        lines.append("")

    # Besonderheiten
    if detail.besonderheiten:
        lines.append("[bold underline]Besonderheiten[/bold underline]")
        for item in detail.besonderheiten:
            lines.append(f"  [yellow]★[/yellow] {_esc(item)}")

    return lines


def price_delta(
    price: float | None, ref_price: float | None
) -> tuple[float, float, str, str] | None:
    """(delta, pct, colour, sign) of a premium vs a reference, or None when either
    side is missing. Shared by the Δ-cell and the favorite pricing block. pct is 0.0
    for a zero reference base (the ratio is undefined; callers render just the €
    delta). Callers apply their own ±0 neutral-band rounding on the returned delta."""
    if price is None or ref_price is None:
        return None
    d = price - ref_price
    pct = d / ref_price * 100 if ref_price else 0.0
    colour = "bright_green" if d < 0 else "bright_red"
    sign = "" if d < 0 else "+"
    return d, pct, colour, sign


def verlauf_row_cells(r: dict) -> tuple[str, str, str, str, str, str, str, str, str]:
    """The nine Verlauf DataTable cells for one joined old/new snapshot row."""
    pos_str = str(r["new_position"]) if r["new_position"] is not None else "—"
    old_p = f"{r['old_price']:.2f}" if r["old_price"] is not None else "—"
    new_p = f"{r['new_price']:.2f}" if r["new_price"] is not None else "—"
    dp = r["delta_price"]
    if dp is not None and dp != 0.0:
        sign = "+" if dp > 0 else ""
        col = "bright_red" if dp > 0 else "bright_green"
        delta_str = f"[{col}]{sign}{dp:.2f}[/{col}]"
        # Only None is a missing base; a real 0.00 old price makes the ratio
        # undefined (∞), so show the € delta without a bogus 0.0% (mirrors
        # snapshot._price_or_worst: 0.0 is a real value, not "missing").
        old_base = r["old_price"]
        if old_base is not None and old_base != 0.0:
            pct = dp / old_base * 100
            pct_str = f"[{col}]{sign}{pct:.1f}%[/{col}]"
        else:
            pct_str = f"[{col}]—[/{col}]"
    elif r["is_new"]:
        delta_str = "[bright_cyan]neu[/bright_cyan]"
        pct_str = "[bright_cyan]—[/bright_cyan]"
    elif r["is_removed"]:
        delta_str = "[dim]weggef.[/dim]"
        pct_str = "[dim]—[/dim]"
    else:
        delta_str = "[dim]—[/dim]"
        pct_str = "[dim]—[/dim]"
    dpos = r["delta_pos"]
    if dpos is not None and dpos != 0:
        dp_sign = "+" if dpos > 0 else ""
        dp_col = "bright_red" if dpos > 0 else "bright_green"
        rank_str = f"[{dp_col}]{dp_sign}{dpos}[/{dp_col}]"
    else:
        rank_str = "[dim]—[/dim]"
    return (pos_str, _esc(r["insurer"]), _esc(r["product"][:38]), _esc(r["sb"]),
            old_p, new_p, delta_str, pct_str, rank_str)
