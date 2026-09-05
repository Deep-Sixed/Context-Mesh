# PR #27 — Session path identity

The stale-writer compare-and-swap protects a session directory, not a lexical path spelling.

A loaded session must refuse a stale save whenever the target is the same underlying directory, including relative, `..`, and symlink aliases. A genuinely different target remains a supported save-as operation.

This note is temporary review documentation for the branch and may be removed before merge.
