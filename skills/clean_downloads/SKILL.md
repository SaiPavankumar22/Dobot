---
name: clean_downloads
description: Organise the Downloads folder without deleting anything.
trigger: clean my downloads, tidy downloads, organise downloads
required_tools: fs_list, fs_move
safety: Never deletes. Unknown files are left in place and reported for the user to review.
---

# Clean Downloads

Sort the user's Downloads folder into an `Archive/` tree so the folder becomes navigable again.

Steps:

1. `fs_list` the folder first and report what is there, grouped by kind (screenshots, documents,
   installers, archives, media, unknown).
2. `fs_move` each known group into `Archive/<Group>` using globs rather than enumerated filenames.
3. Never delete. If the user explicitly asks for deletion, that is a separate request which requires
   approval.
4. Report the counts per group and name the files that were left alone.

Safety requirements:

- Moving is allowed automatically; deleting is not.
- `on_conflict: rename` so nothing is overwritten.
- Anything under 1 MB that is not recognisable stays where it is and is reported.
