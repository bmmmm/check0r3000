"""check0r3000 — pure text-formatting helpers.

Textual-free leaf module: every function here returns plain strings (some carry
Textual markup tags as literal text). Row/record arguments are duck-typed, so
this module imports no data model and pulls in no Textual."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotations only; never imported at runtime (keeps this leaf
    from tui_data import SnapshotRow  # module Textual-free and data-model-free)


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


def _bewertung_cell(row: SnapshotRow) -> str:
    """CHECK24 customer rating (0-5 stars), distinct from the expert Tarifnote.
    Shows '—' until a scrape with rating support has populated the snapshot."""
    if row.bewertung is None:
        return "[dim]—[/dim]"
    v = row.bewertung
    color = "bright_green" if v >= 4.5 else "yellow" if v >= 3.5 else "bright_red"
    val = f"{v:.1f}".replace(".", ",")
    cnt = f" [dim]({row.bewertung_anzahl})[/dim]" if row.bewertung_anzahl else ""
    return f"[{color}]{val}★[/{color}]{cnt}"


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


def _module_cell(mod: dict[str, Any]) -> tuple[str, str]:
    """(plain, colour) for one module in a tariff column."""
    if not isinstance(mod, dict) or not mod.get("included"):
        return "—", "dim"
    return {
        "Premium": ("★★★ Premium", "bright_green"),
        "Komfort": ("★★ Komfort", "yellow"),
        "Basis": ("★ Basis", "white"),
    }.get(mod.get("level"), ("✓", "cyan"))


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
