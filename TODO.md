# TODO

Everything from the security + UI review has been fixed and verified; those
entries have been removed rather than kept as a changelog. What follows is
what is still open.

## Open

- [ ] **Polygon zones redact the wrong area.** Deleting everything in the
      bounding box is not the intended behaviour: only what is *inside the
      drawn shape* should be removed. Over-deleting text is acceptable if the
      implementation needs it — what is not acceptable is covering the whole
      bounding box in white, since the visible result must follow the outline.
      (The white cover already follows the exact outline today; it is the
      *deletion* that runs on the bounding box, and the dashed rectangle now
      drawn around the zone is what makes that visible.)

      PyMuPDF can only redact rectangles, so following the shape means
      decomposing it into horizontal strips and redacting each one — more
      code, slower exports, and still slightly over-deleting at strip edges.

      Counterpoint, from the same discussion: **the polygon tool has strictly
      no interest.** If that holds, the cheaper resolution is to drop the
      polygon tool rather than build strip decomposition for it. Freehand has
      the same bounding-box behaviour and would need the same decision.
      To settle with the user before touching either.

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

- [ ] **The inspector reads the document once, at open.** If a redaction ever
      became something you could undo *after* export, or if the file were
      reloaded, the pane would be stale. Not a problem today — nothing mutates
      the session document — but worth remembering before adding anything that
      does.
