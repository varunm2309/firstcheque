"""India's RDI (Research, Development and Innovation) Scheme priority themes.

The original TypeScript reference (`lib/rdi.ts`) that this was meant to be
ported from could not be found anywhere on this machine (checked the
working tree and a full-disk filename search for `rdi.ts`). This list was
built independently by reading the scheme's own site rather than guessing:

  - RDI Fund official site (Anusandhan National Research Foundation):
    https://rdifund.anrf.gov.in/  (accessed 2026-09-17)
  - Drishti IAS explainer, cross-checked against the ESTIC 2025 thematic
    framework for the broader phrasing of a couple of themes:
    https://www.drishtiias.com/daily-updates/daily-news-analysis/research-development-and-innovation-rdi-scheme
    (accessed 2026-09-17)

The RDI Scheme (Rs 1 lakh crore corpus, approved by the Union Cabinet on
2025-07-01, Department of Science & Technology as nodal department) funds
private-sector R&D in "sunrise domains and sectors relevant for economic
security, strategic purpose, and self-reliance." It does not, as far as
either source states, publish a single canonical numbered list of themes
with fixed IDs -- the list below is FirstCheque's own working taxonomy for
tagging memos, not an official enumeration. Treat it as a starting point to
re-verify against the scheme's operational guidelines when they are
published, not as a citation-grade legal list.
"""

from __future__ import annotations

from pydantic import BaseModel


class RDITheme(BaseModel):
    id: str
    name: str
    description: str


RDI_THEMES: list[RDITheme] = [
    RDITheme(
        id="energy_climate",
        name="Energy security, energy transition and climate action",
        description="Clean energy, energy storage, grid tech, climate adaptation and mitigation technology.",
    ),
    RDITheme(
        id="ai",
        name="Artificial intelligence and applied AI",
        description="AI and its applications across agriculture, health and education, per the scheme's own framing.",
    ),
    RDITheme(
        id="quantum",
        name="Quantum technologies",
        description="Quantum computing, communication and sensing.",
    ),
    RDITheme(
        id="robotics",
        name="Robotics",
        description="Industrial, service and field robotics.",
    ),
    RDITheme(
        id="biotech",
        name="Biotechnology, biomanufacturing and med-tech",
        description="Biotechnology, biomanufacturing, synthetic biology, pharmaceuticals and medical devices.",
    ),
    RDITheme(
        id="space",
        name="Space technology",
        description="Launch, satellite, and downstream space-data technologies.",
    ),
    RDITheme(
        id="digital_economy",
        name="Digital economy, including digital agriculture",
        description="Digital public infrastructure, fintech rails, and digital agriculture platforms.",
    ),
    RDITheme(
        id="semiconductors_electronics",
        name="Semiconductors and electronics manufacturing",
        description=(
            "Semiconductor design/fab and electronics manufacturing -- widely reported as a sunrise "
            "priority sector alongside the RDI Scheme, though not itemised on the RDI Fund site's own "
            "theme list checked here; flag this theme as lower-confidence than the seven above."
        ),
    ),
]

_THEMES_BY_ID = {t.id: t for t in RDI_THEMES}


def get_theme(theme_id: str) -> RDITheme:
    try:
        return _THEMES_BY_ID[theme_id]
    except KeyError as exc:
        raise ValueError(f"unknown RDI theme id: {theme_id!r}") from exc


def suggest_themes(text: str, limit: int = 2) -> list[RDITheme]:
    """Very rough keyword match against theme names/descriptions, to surface
    CANDIDATES only. This is not a classifier and must not be trusted as a
    final theme assignment -- the memo author (Claude, under skill
    instructions) still has to write a specific rationale for whichever
    theme(s) it keeps, and the memo may keep at most 2.
    """
    text_lower = text.lower()
    scored = []
    for theme in RDI_THEMES:
        haystack = f"{theme.name} {theme.description}".lower()
        score = sum(1 for word in haystack.split() if len(word) > 4 and word in text_lower)
        if score > 0:
            scored.append((score, theme))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [theme for _, theme in scored[:limit]]
