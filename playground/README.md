# REST Client JMESPath Playground

A local website for pasting a REST JSON response and testing JMESPath
expressions (and full mapping documents) with **the same engine the plugin
uses**.

Vanilla [jmespath.org](https://jmespath.org/) cannot run this plugin's
extensions:

- `from_json(@)` - parse a stringified-JSON field
- `to_array(@)` - object -> `[{key, value}, …]`
- `flatten(@[, identity_keys[, separator]])` - nested JSON -> joined leaf paths (default `_`)
- `$item` / `$parent` - `for_each` iteration variables
- user-defined functions - paste Python `def`s in the **User functions** box

This playground calls `extraction.evaluate()` / `extraction.extract()`
directly, so a query that works here works in the plugin.

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
cd playground
uv run uvicorn app:app --reload --port 8765
```

Open <http://127.0.0.1:8765>.

Or:

```bash
cd playground
uv run python app.py
```

## Usage

1. Paste a REST response into the JSON pane, or pick a fixture example.
2. **Expression** - type a JMESPath query. Completions after `.` walk the
   live document through the plugin engine, so `from_json(Data).` and
   `flatten(from_json(Data), …).` can offer the inner keys. Check *Bind
   `$item` / `$parent`* to evaluate `for_each` expressions against the
   document root.
3. **User functions** - open the box and paste top-level Python `def`s.
   They become JMESPath functions for the Expression tab (and for Mapping
   when the YAML has no `functions:` block). Try `flatten_hierarchy` or
   `site_snapshot` in the example picker for a `flatten()` query.
4. **Mapping** - paste a YAML mapping document and see the
   `(asset, datapoint, value, timestamp)` tuples the plugin would emit.

## Tests

```bash
cd playground
uv run pytest
```
