"""
Generate tenant-specific CSS custom properties from SiteSettings.
"""

FONT_PAIRINGS = {
    "inter-inter": {"heading": "Inter", "body": "Inter"},
    "poppins-inter": {"heading": "Poppins", "body": "Inter"},
    "dm-sans-dm-sans": {"heading": "DM Sans", "body": "DM Sans"},
    "playfair-source": {"heading": "Playfair Display", "body": "Source Sans 3"},
    "space-grotesk-work": {"heading": "Space Grotesk", "body": "Work Sans"},
    "cormorant-fira": {"heading": "Cormorant Garamond", "body": "Fira Sans"},
}

TYPE_SCALES = {
    "compact": {"base": 16, "ratio": 1.2},
    "default": {"base": 18, "ratio": 1.25},
    "spacious": {"base": 20, "ratio": 1.333},
}

DEFAULT_PALETTE = [
    "oklch(0.45 0.15 160)",
    "oklch(0.70 0.12 85)",
    "oklch(0.35 0.10 200)",
    "oklch(0.55 0.08 250)",
]


def generate_site_css(settings):
    """
    Generate a CSS string with :root custom properties from a SiteSettings instance.

    Includes palette, neutrals, fonts, type scale, spacing, and border radius.
    """
    palette = settings.palette if settings.palette else DEFAULT_PALETTE
    pairing = FONT_PAIRINGS.get(settings.font_pairing, FONT_PAIRINGS["inter-inter"])
    scale = TYPE_SCALES.get(settings.type_scale, TYPE_SCALES["default"])

    border_radius = max(0, min(64, settings.border_radius))
    space_unit = max(4, min(32, settings.space_unit))

    base = scale["base"]
    ratio = scale["ratio"]

    lines = [":root {"]

    # Palette colors
    for i, color in enumerate(palette[:4], start=1):
        lines.append(f"  --palette-{i}: {color};")

    # Neutrals
    lines.append("  --neutral-white: oklch(0.99 0 0);")
    lines.append("  --neutral-black: oklch(0.15 0 0);")
    lines.append("  --neutral-light: oklch(0.95 0.005 90);")
    lines.append("  --neutral-dark: oklch(0.40 0 0);")
    lines.append("")

    # Fonts
    lines.append(f'  --font-heading: "{pairing["heading"]}", system-ui, sans-serif;')
    lines.append(f'  --font-body: "{pairing["body"]}", system-ui, sans-serif;')
    lines.append("")

    # Type scale
    lines.append(f"  --base-size: {base}px;")
    lines.append(f"  --type-scale: {ratio};")
    lines.append("")

    # Spacing
    lines.append(f"  --space-unit: {space_unit}px;")
    lines.append(f"  --space-xs: {space_unit // 2}px;")
    lines.append(f"  --space-sm: {space_unit}px;")
    lines.append(f"  --space-md: {space_unit * 2}px;")
    lines.append(f"  --space-lg: {space_unit * 3}px;")
    lines.append(f"  --space-xl: {space_unit * 4}px;")
    lines.append(f"  --space-2xl: {space_unit * 6}px;")
    lines.append(f"  --space-3xl: {space_unit * 8}px;")
    lines.append("")

    # Border radius
    lines.append(f"  --radius-sm: {max(1, border_radius // 2)}px;")
    lines.append(f"  --radius-md: {border_radius}px;")
    lines.append(f"  --radius-lg: {min(64, border_radius * 2)}px;")
    lines.append("")

    lines.append("  --max-width: 1200px;")
    lines.append("}")

    return "\n".join(lines)


def get_google_fonts_url(settings):
    """
    Return the Google Fonts URL for the selected font pairing.
    """
    pairing = FONT_PAIRINGS.get(settings.font_pairing, FONT_PAIRINGS["inter-inter"])
    heading = pairing["heading"]
    body = pairing["body"]

    families = []
    for font in dict.fromkeys([heading, body]):  # deduplicate, preserve order
        family = font.replace(" ", "+")
        families.append(f"family={family}:wght@400;500;600;700")

    params = "&".join(families)
    return f"https://fonts.googleapis.com/css2?{params}&display=swap"


def resolve_color(color_ref, settings):
    """
    Resolve a color reference to a CSS value.

    Supports:
      - "palette-1" through "palette-4" → the matching palette color
      - "white" → oklch(1.00 0 0)
      - "black" → oklch(0.00 0 0)
      - "transparent" → transparent
      - Raw oklch() values → passed through unchanged
    """
    if not color_ref:
        return "transparent"

    color_ref = color_ref.strip()

    # Named colors
    if color_ref == "white":
        return "oklch(1.00 0 0)"
    if color_ref == "black":
        return "oklch(0.00 0 0)"
    if color_ref == "transparent":
        return "transparent"

    # Palette references
    if color_ref.startswith("palette-"):
        try:
            index = int(color_ref.split("-")[1]) - 1
            palette = settings.palette if settings.palette else DEFAULT_PALETTE
            if 0 <= index < len(palette):
                return palette[index]
        except (ValueError, IndexError):
            pass
        return "transparent"

    # Raw oklch() or other CSS color - pass through
    return color_ref
