# Example mapping documents

Working mapping documents for `fledge-south-restclient`, each verified against
a real payload shape. Copy one, point it at your endpoint, and edit.

The DSL reference (JMESPath basics, `from_json()`, `to_array()`,
`flatten()`, `functions:`, `for_each`, `from`) is in
[the main README](../README.md#the-mapping-document).

| File | Payload shape | Demonstrates |
|---|---|---|
| [`site-snapshot-full-mapping.yaml`](site-snapshot-full-mapping.yaml) | OData v4 collection whose `Data` field is stringified JSON containing a site -> lines -> equipment hierarchy | `from_json()`, `for_each` with `$item`/`$parent`, asset-name templating, `from` pass-through, two rules merging into one asset |
| [`site-snapshot-flatten-mapping.yaml`](site-snapshot-flatten-mapping.yaml) | Same snapshot shape | `flatten()` + `from`: one joined datapoint per leaf (`SITE-A_Line1_Tank01_Readings_Level`) without a rule per object |
| [`site-snapshot-tag-map.yaml`](site-snapshot-tag-map.yaml) | Same snapshot shape | Read tag map (`tags`) allowlist: flattened keys -> `tank-01` / `pump-02` assets |
| [`user-functions-mapping.yaml`](user-functions-mapping.yaml) | Flat object with a display name and a °F reading | Mapping `functions:` block - `slug()` in the asset template, `f_to_c()` on the value |

More fixture-scale examples live under [`tests/fixtures/mappings/`](../tests/fixtures/mappings/),
each paired with the payload it consumes and the readings it is expected to
produce. Those are the executable specification - if you want to know exactly
what a DSL feature does, read the fixture triple.

---

## Using an example

Set these two items in the south service configuration:

| Item | Value |
|---|---|
| `mappingSource` | `file` |
| `mappingFile` | absolute path to the `.yaml` file |

The mapping is parsed and validated at `plugin_init`. A malformed document is a
**hard startup error** - the plugin will not run with a broken mapping, rather
than silently emitting nothing. Check the Fledge syslog if the service fails to
start.

Small mappings can instead be pasted into `mappingInline` with
`mappingSource=inline`, which keeps them versioned with the Fledge
configuration rather than on the filesystem. Anything longer than a screen is
easier to review and diff as a file.

---

## `site-snapshot-full-mapping.yaml`

Polls an OData v4 endpoint that returns a telemetry snapshot of a site. Each
OData record carries a **stringified JSON** `Data` field holding the
hierarchy:

```
value[]                       OData records
  .LastUpdated                one timestamp for every value in the record
  .Data                       STRINGIFIED JSON -> from_json()
    .SiteId                   "SITE-A"
    .Lines[]                  .LineId, plus an Equipment[] list
      .Equipment[]            .EquipmentId, .Type, and two objects of values:
                              Readings, Status
```

It emits **one asset per piece of equipment** - `SITE-A-Pump01` - with every
field of `Readings` and `Status` as a datapoint. Against the reference
payload: 2 rules producing 5 assets.

**Try it without the endpoint.** `simulator/serve_fixtures.py` serves a
simulated site of the same shape at
`http://127.0.0.1:4444/site_snapshot.json` - tanks filling and draining,
pumps starting and stopping, evolving on a 15-second tick, with every name
invented. This mapping is what the `site_snapshot` fixture triple runs, so the
example is regression-tested rather than merely documented. See
[`docs/rest-testing.rst`](../docs/rest-testing.rst).

Three patterns in it are worth reusing:

**Rehydrating stringified JSON.** `from_json(Data)` parses a JSON string
embedded in a JSON field. Industrial REST APIs do this constantly. Inside a
`for_each`, reach back to the enclosing record for it:
`from_json($parent.Data).SiteId`.

**Flattening a two-level hierarchy into one `for_each`.** `for_each` nests only
one level, so the line and equipment loops are collapsed into a single
JMESPath projection:

```yaml
for_each: "from_json(Data).Lines[].Equipment[]"
```

Add a filter such as `Lines[?LineId=='Line1']` to limit it to one line.

**Merging objects into one asset.** Two rules share an asset name, each
expanding a different object with `from`. They merge into a single reading,
because the emission layer groups on `(asset, timestamp)`:

```yaml
  - for_each: &equipment "from_json(Data).Lines[].Equipment[]"
    asset: &asset "{from_json($parent.Data).SiteId}-{$item.EquipmentId}"
    timestamp: &ts "$parent.LastUpdated"
    from: "$item.Readings"

  - for_each: *equipment
    asset: *asset
    timestamp: *ts
    from: "$item.Status"      # same asset -> merges into the reading above
```

Because `from` discovers datapoint names from the data, a new value appearing
in the payload is picked up with no mapping change, and equipment missing from
a poll simply produces no reading rather than an error.

### Adapting it

- **`&name` / `*name` are YAML anchors**, not plugin syntax. They resolve at
  parse time to avoid repeating the long JMESPath expressions; expand them by
  hand if you prefer.
- **The `cast:` block is deliberate.** A reading such as `FlowRate` can arrive
  as integer `0` when a pump is stopped and as a float when it is running - the
  same value changing type between polls, which some historians dislike.
  `cast: {"*": float}` pins every `Readings` value to `float`; `Status` flags
  are left as integers.
- **Check for datapoint-name collisions** when you add an object to an asset.
  Objects merging into one reading are last-write-wins on datapoint name, so a
  name appearing in two objects silently loses one value. The two objects in
  this example do not collide.
- **Timestamps are normalized for Fledge.** This source emits ISO-8601 with a
  `T` and 7 fractional-second digits (`2026-08-04T13:05:45.7308710Z`). Fledge's
  C++ ingest parses only `YYYY-MM-DD HH:MM:SS`, so a `T` would file every
  reading at midnight UTC, and it overflows on more than 6 digits. The plugin
  therefore converts every timestamp to UTC and emits
  `2026-08-04 13:05:45.730871+00:00`, truncating to microseconds.
