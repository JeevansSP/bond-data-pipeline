# Documentation conventions

Every doc you write must follow these rules.

## Filename

Name the file `<created_timestamp_IST>_<doc_title>.md`, where the timestamp marks when the doc
was first created (IST) and never changes afterward.

Example: `2026-07-18_112508_bondcentral-api-exploration.md`

## Header block

The first line of every doc must be a fenced code block containing, in order:

1. A short description of what the doc covers.
2. A blank line.
3. One changelog entry per update, each on its own line, in the form
   `<updated_at_timestamp_IST> : <update_message>`.

Append a new changelog line for every subsequent edit — do not rewrite existing entries.

```
<short description of the doc>

<updated_at_timestamp_IST> : <update message>
<updated_at_timestamp_IST> : <update message>
...
```
