# The MCP server's icons (F1215)

What `initialize` names Synapse by, for an AI tool that draws a server's icon (VS Code's tools picker).

- `mark-dark-48.png`: for a dark theme. The mark from `frontend/public/logo.svg`, 86% of the square, on a
  white rounded square (corner radius 20%).
- `mark-light-48.png`: for a light theme. The same mark alone, on transparent.

PNG, because VS Code draws PNG, WebP, JPEG or GIF and never SVG. 48 × 48, because a tool picks the smallest
icon at least as big as it draws (VS Code: 22). Naveen chose both on 2026-10-29. To remake them at another
size, render `logo.svg`, crop it to the mark, and scale its taller side to 86% of the square.
