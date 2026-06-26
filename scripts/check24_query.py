#!/usr/bin/env -S uv run
"""Build a CHECK24 Rechtsschutz result URL from the saved quote profile.

CHECK24 has no JSON results API: the comparison is rendered server-side from the URL
query string, so the query IS the payload. `config/check24-profile.json` stores your
exact query verbatim; this script parses it and overrides ONLY the levers you ask for,
so every other default you picked on CHECK24 is reproduced unchanged.

Run:
  uv run scripts/check24_query.py --show                 # decode the key levers
  uv run scripts/check24_query.py                        # your saved URL, unchanged
  uv run scripts/check24_query.py --all-insurers         # drop the single-insurer pin
  uv run scripts/check24_query.py --provider 11          # pin one insurer by id
  uv run scripts/check24_query.py --position 4 --costsharing 1000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "config" / "check24-profile.json"
EXAMPLE = ROOT / "config" / "check24-profile.example.json"
PROVIDERS = ROOT / "config" / "check24-providers.json"

# Params that pin the result list to one insurer / package. --all-insurers drops them.
PIN_KEYS = ("provider_filter", "tariff_package", "tariff_position")
# module_* flag -> human label, for --show.
MODULE_LABELS = {
    "module_priv": "Privat", "module_job": "Beruf", "module_traffic": "Verkehr",
    "module_living": "Wohnen", "module_rental": "Vermietung",
}


def load_profile() -> tuple[dict, bool]:
    """Return (profile, is_example). Falls back to the tracked example with a warning
    so a fresh checkout still produces a URL instead of crashing."""
    if PROFILE.exists():
        return json.loads(PROFILE.read_text(encoding="utf-8")), False
    if EXAMPLE.exists():
        print(f"! {PROFILE.relative_to(ROOT)} not found — using the example profile "
              f"(fake birthdate/zipcode). Copy the example and edit it.", file=sys.stderr)
        return json.loads(EXAMPLE.read_text(encoding="utf-8")), True
    sys.exit(f"No profile: create {PROFILE.relative_to(ROOT)} "
             f"(copy {EXAMPLE.relative_to(ROOT)}).")


def provider_name(pid: str) -> str | None:
    if not PROVIDERS.exists():
        return None
    table = json.loads(PROVIDERS.read_text(encoding="utf-8")).get("providers", {})
    return table.get(str(pid))


def set_param(pairs: list[tuple[str, str]], key: str, value: str) -> list[tuple[str, str]]:
    """Replace every occurrence of `key` with a single key=value (append if absent)."""
    out = [(k, v) for k, v in pairs if k != key]
    out.append((key, value))
    return out


def decode_discounts(pairs: list[tuple[str, str]]) -> list[str]:
    raw = dict(pairs).get("discounts")
    if not raw:
        return []
    try:
        items = json.loads(raw.replace("&quot;", '"'))
    except (json.JSONDecodeError, AttributeError):
        return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    # Guard per element so one wrong-shape entry cannot drop the whole list and make
    # --show silently disagree with the URL it emits.
    return [d.get("name") for d in items
            if isinstance(d, dict) and d.get("value") == "yes"]


def show(pairs: list[tuple[str, str]]) -> None:
    d = dict(pairs)  # last value wins (matches browser semantics for repeated keys)
    # CHECK24 repeats some keys (e.g. module_priv twice). Last-wins is correct, but
    # warn if a repeated key carries *conflicting* values — then the display is lossy.
    multi: dict[str, set] = {}
    for k, v in pairs:
        multi.setdefault(k, set()).add(v)
    conflicting = sorted(k for k, vs in multi.items() if len(vs) > 1)
    pid = d.get("provider_filter")
    pin = "ALL insurers" if not pid else f"{pid} ({provider_name(pid) or 'unknown id'})"
    modules = [lbl for k, lbl in MODULE_LABELS.items() if d.get(k) == "yes"]
    print("Profile levers:")
    print(f"  provider_filter : {pin}")
    print(f"  tariff_package  : {d.get('tariff_package', '(none)')}")
    print(f"  maritalstatus   : {d.get('maritalstatus')}")
    print(f"  birthdate       : {d.get('birthdate')}")
    print(f"  zipcode         : {d.get('zipcode')}")
    print(f"  employment      : {d.get('employmentstatus')} / partner {d.get('employmentstatus_partner')}")
    print(f"  modules         : {', '.join(modules) or '(none)'}")
    print(f"  costsharing     : {d.get('costsharing')}")
    print(f"  stiftung_wt     : {d.get('stiftung_warentest')}")
    print(f"  discounts       : {', '.join(decode_discounts(pairs)) or '(none)'}")
    print(f"  sort            : {d.get('sortfield')} {d.get('sortorder')}")
    if conflicting:
        print(f"  ! repeated keys with differing values (showing last): {', '.join(conflicting)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a CHECK24 RSV result URL from the profile.")
    # --all-insurers and --provider are contradictory (one widens, one pins): make them
    # mutually exclusive so passing both fails loudly instead of silently re-pinning.
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all-insurers", action="store_true",
                   help="drop provider_filter/tariff_package/tariff_position -> every insurer")
    g.add_argument("--provider", metavar="ID",
                   help="pin one insurer by provider_filter id (drops tariff_package)")
    ap.add_argument("--position", metavar="N", help="set tariff_position")
    ap.add_argument("--costsharing", metavar="V", help="set costsharing (SB tolerance)")
    ap.add_argument("--show", action="store_true", help="print decoded levers instead of a URL")
    args = ap.parse_args()

    if args.provider is not None and not args.provider.strip():
        ap.error("--provider needs an id; use --all-insurers to drop the pin")

    profile, _ = load_profile()
    base = profile.get("base_url")
    query = profile.get("query")
    if not base or query is None:
        sys.exit(f"Profile is missing base_url/query — see {EXAMPLE.relative_to(ROOT)}.")

    pairs = parse_qsl(query, keep_blank_values=True)

    if args.all_insurers:
        pairs = [(k, v) for k, v in pairs if k not in PIN_KEYS]
    if args.provider:
        # drop both single-insurer pins: a stale package/position from the old
        # result list is meaningless once the provider changes.
        pairs = [(k, v) for k, v in pairs if k not in ("tariff_package", "tariff_position")]
        pairs = set_param(pairs, "provider_filter", args.provider)
    # Distinguish "flag absent" (None) from "explicit blank" ('') so --position '' can
    # intentionally clear a pin instead of being silently swallowed by a truthiness test.
    if args.position is not None:
        pairs = set_param(pairs, "tariff_position", args.position)
    if args.costsharing is not None:
        pairs = set_param(pairs, "costsharing", args.costsharing)

    if args.show:
        show(pairs)
        return 0

    print(base + "?" + urlencode(pairs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
