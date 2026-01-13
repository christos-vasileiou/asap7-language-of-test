import regex as re

def best_match(files, cat, VARIANT, PVT):
    # Compile a category-specific regex once
    pat = re.compile(
        rf"{re.escape(cat)}_{re.escape(VARIANT)}_{re.escape(PVT)}(?:_(ccs(?![an])|ccsa|ccsn))?",
        re.IGNORECASE
    )

    # Score: lower is better
    def score(suffix):
        # suffix is 'ccs', 'ccsa', 'ccsn', or None (base only)
        if suffix == "ccs":     return (0, )
        if suffix is None:      return (1, )
        if suffix in ("ccsa","ccsn"):
            # Prefer ccsa over ccsn only if you need a tie-break
            return (2, 0 if suffix == "ccsa" else 1)
        return (3, )

    candidates = []
    for f in files:
        m = pat.search(f.name)
        if m:
            sfx = m.group(1)
            # Normalize 'ccs' captured inside '(ccs(?![an])|...)'
            if sfx and sfx.startswith("ccs") and sfx not in ("ccs", "ccsa", "ccsn"):
                sfx = "ccs"
            candidates.append((score(sfx), f))

    if not candidates:
        return None
    return str(min(candidates, key=lambda t: t[0])[1])