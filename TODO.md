# TODO

Everything from the security + UI review has been fixed and verified; those
entries have been removed rather than kept as a changelog. What follows is
what is still open.

## Open

- [ ] **Scroll synchronisation between the two panes needs a decision.**
      Today it is one-way and coarse: when the page you are reading on the
      left changes, the right pane scrolls to that page's sheet. Two problems
      follow. A sheet is nothing like the height of the page it describes, so
      "the same place" is only ever the top of the sheet. And there is no
      reverse sync — scrolling the right pane to read page 5 while the left
      pane still shows page 1 means the smallest scroll on the left yanks the
      right pane back, which reads as the panel fighting you. The heuristic
      that guards this (a 400 ms `syncingScroll` flag) is a guess, not a fix.
      Options, to settle with the user: leave it one-way as now, make it
      two-way with a proper lock, or drop the syncing entirely and let each
      pane scroll on its own.

- [ ] **Touch drawing is discoverable only from the help popup.** The page
      layer uses `touch-action: pan-y`, so a vertical drag scrolls and a zone
      has to be started with a sideways movement. It works, but it is a trick
      that needs explaining. An explicit "naviguer / dessiner" toggle, shown
      only for coarse pointers, would be honest instead of clever.

- [ ] **The inspector reads the document once, at open.** If a redaction ever
      became something you could undo *after* export, or if the file were
      reloaded, the pane would be stale. Not a problem today — nothing mutates
      the session document — but worth remembering before adding anything that
      does.
