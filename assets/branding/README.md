# Application icon

The speech bubble combines an audio waveform with transcript lines. The design uses sapphire blue, turquoise and white, without text or third-party brand marks. Transparency outside the rounded square is retained in every export.

| File | Use |
| --- | --- |
| `kolejka-transkrypcji-oauth-120.png` | 120 by 120 PNG for the Google OAuth branding form |
| `kolejka-transkrypcji-512.png` | Larger PNG for documentation and app previews |
| `kolejka-transkrypcji.ico` | Windows icon containing 16, 24, 32, 48, 64, 128 and 256 pixel frames |
| `kolejka-transkrypcji-master.png` | Original generated artwork, preserved without re-encoding |

Google recommends a square 120 by 120 image in PNG, JPG or BMP format, no larger than 1 MB: [OAuth app branding requirements](https://support.google.com/cloud/answer/15549049?hl=en).

Created on 2026-11-14 using the built-in `image_gen` tool with `transparent_background=true`. Exports only resize and encode the original artwork; no separate API/CLI generation was used.

## Generation prompt

> Use case: logo-brand. Asset type: final app icon for the Windows desktop application Kolejka transkrypcji and its Google OAuth consent screen. Create one original polished icon, not a presentation or mockup. A square 1024 by 1024 composition. Center one deep sapphire blue rounded square tile with generous rounded corners and a small even transparent margin around it. Within the tile, a bold clean white rounded speech/document shape, with one short lower-left speech tail. Inside this white shape, the upper half contains five thick turquoise vertical audio waveform pill bars of alternating heights, balanced around the tallest center bar. The lower half contains two short dark sapphire horizontal rounded text lines, one longer and one shorter, clearly representing transcribed subtitles. Very simple confident flat vector-like geometry, optically centered, harmonious professional proportions, strong contrast, enough breathing room and thick details to remain recognizable at 120x120 and 32x32. Flat solid colors: sapphire, turquoise, white. Crisp smooth contours. Transparent outside the tile. No text, no letters, no wordmark, no Google or YouTube branding, no microphone drawing, no decorative objects, no gradients, no drop shadows, no 3D, no bevel, no photographic texture, no border around the canvas. Just one finished emblem.

The tool returned a 1280 by 1280 master. The OAuth PNG is exported at exactly 120 by 120.
