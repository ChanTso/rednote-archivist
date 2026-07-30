# Selector Strategy

The collector uses conservative URL and semantic heuristics:

- Note links: anchors whose `href` matches `/explore/<note_id>` or `/discovery/item/<note_id>`.
- Login detection: absence of common login modal text plus presence of user/account/navigation signals.
- Carousel images: prefer images inside article/detail/dialog regions; do not use all page images as final proof.

Platform markup can change without notice. Any selector change must be verified against an authorized live session and reflected in synthetic fixtures and parser tests. Never commit captured production pages.
