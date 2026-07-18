# Landing page redesign: man-page brutalism

Date: 2026-07-19
Status: approved direction (dark terminal, man page + affordances)

## Motivation

The current page is a generic dark glow-gradient dev-tool template. Replace it
with a distinctive design: the landing page rendered as `man hc`. Content is
also stale (Codex/Claude only, pipx only) and gets updated in the same pass.

## Design

One static file, `landing/index.html`, rewritten in place.

### Structure (top to bottom)

1. Corner nav line (GitHub, Install anchors), then the man header line:
   `HC(1)    User Commands    HC(1)`.
2. NAME: `hc - move a coding session between agents and resume it natively`.
3. SYNOPSIS: real CLI grammar:
   - `hc --from HARNESS --to HARNESS [SESSION] [--write] [--dest-cwd DIR]`
   - `hc list --from HARNESS`
4. DESCRIPTION: escape-hatch story, 2 short paragraphs (rate-limited at 80%,
   transcript read off disk, dead harness never needs to run again).
5. EXAMPLES: the rescue transcript as man example blocks; error line in red,
   prompts in green, ends with the printed resume command.
6. HOW IT WORKS: four indented paragraphs with bold run-in headings: common
   interface (4 records), N^2 enrichment, dual streams, ragged tails.
7. INSTALLATION: three commands, each with a [copy] affordance:
   - `pipx install harness-convert`
   - `npm i -g @theharshitsingh/hc`
   - `brew install harshitsinghbhandari/tap/harness-convert`
8. SUPPORTED HARNESSES: Codex, Claude Code, OpenCode (with the
   `opencode import` note).
9. SEE ALSO / footer: GitHub, author site, MIT, closing man-footer line
   `harness-convert 0.2.0`.

### Visual system

- Near-black background, off-white text, single monospace stack, ~80ch max
  width.
- ALL-CAPS section headers flush left, content indented (man convention).
- Bold = literals, underline = placeholders. One green accent, red for error
  lines only.
- No gradients, glows, cards, shadows, or rounded corners.

### Affordances (the only web-isms)

- Click-to-copy `[copy]` text on the three install lines (small inline JS).
- Section headers are anchor links.
- Responsive: font size steps down on narrow viewports; lines wrap.

### Kept as-is

- Meta description / OG tags (copy updated to mention all three harnesses),
  CNAME, GitHub Pages deploy workflow, favicon.

## Error handling / testing

Static page; nothing to error. Verify by opening the file locally, checking a
~375px viewport, confirming copy buttons work, then push (Pages deploys on
push to main).

## Out of scope

Multi-page docs, light theme, analytics, screenshots/video.
