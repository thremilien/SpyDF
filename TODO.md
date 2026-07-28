# TODO

Everything from the security + UI review has been fixed and verified; those
entries have been removed rather than kept as a changelog. What follows is
what is still open.

## Open

- [ ] Add the possibility to add filigrane on pages
- [ ] Add logs on connection to the site, import pdf and export pdf

## Closed

- [x] **Polygon zones redact the wrong area.** Settled: the tools stay, and
      deletion now follows the drawn outline. `_zone_rects` cuts a polygon or
      freehand zone into ~2 pt horizontal strips (200 max) and redacts each
      one; the mosaic of a "repixeliser" zone is clipped to the same outline by
      an alpha mask. Over-deleting a little is accepted, whitening outside the
      shape is not. The dashed bounding box has been removed from the UI, since
      it no longer describes anything.

- [x] **Scroll synchronisation between the two panes.** Settled: the inspector
      now redraws every page at the size of the one on the left, with each
      hidden element at its real position, so a reading position means "page n,
      x % down" in both panes. Sync is two-way, and the 400 ms flag is gone —
      the pane being scrolled owns the lock and releases it on `scrollend`.
      Document-level traces, which have no position, moved to a column on the
      far left of the workspace so that both page columns keep the same width.
