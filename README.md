# fledge-south-restclient

A [Fledge](https://github.com/fledge-iot) **south** plugin that polls HTTP/REST
endpoints (including OData v4 services) and turns their JSON responses into
Fledge readings using a **declarative, JMESPath-based mapping document**.

All of the reshaping logic lives in configuration: the only thing that changes
per deployment is a mapping document, and optional Python helpers live in that
document as JMESPath functions rather than in a chained filter plugin.

- **Plugin name:** `restclient`
- **Type / mode:** `south` / `poll`
- **Fledge interface:** `2.0`
- **License:** Apache 2.0
- **Developed by:** Industrial Intelligence Group

---

## Contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration reference](#configuration-reference)
- [The mapping document](#the-mapping-document)
- [JMESPath: what it is and how this plugin uses it](#jmespath-what-it-is-and-how-this-plugin-uses-it)
  - [JMESPath playground](#jmespath-playground)
  - [Custom functions: `from_json()`, `to_array()` and `flatten()`](#custom-functions-from_json-to_array-and-flatten)
  - [Read tag map](#read-tag-map)
  - [User-defined functions](#user-defined-functions)
  - [Template substitution in names](#template-substitution-in-names)
  - [Iteration with `for_each`, `$item` and `$parent`](#iteration-with-for_each-item-and-parent)
  - [Pass-through with `from`, `rename`, `cast` and `exclude`](#pass-through-with-from-rename-cast-and-exclude)
- [Worked examples](#worked-examples)
- [Reading structure modes](#reading-structure-modes)
- [Timestamps](#timestamps)
- [Error handling](#error-handling)
- [OData v4 handling](#odata-v4-handling)
- [Security notes](#security-notes)
- [Development & testing](#development--testing)

---

## How it works

Each poll cycle runs three internally-decoupled layers:

```
   HTTP endpoint
        │
        ▼
┌──────────────────┐   GET, auth, TLS, timeout, OData pagination,
│  Acquisition     │   response-size cap -> one JSON document
└──────────────────┘
        │  JSON payload
        ▼
┌──────────────────┐   apply the mapping document with JMESPath ->
│  Extraction      │   a stream of (asset, datapoint, value, timestamp) tuples
└──────────────────┘
        │  tuples
        ▼
┌──────────────────┐   group tuples into Fledge reading dicts according to
│  Emission        │   readingStructure -> list[ {asset, timestamp, key, readings} ]
└──────────────────┘
        │
        ▼
   Fledge readings
```

The **mapping document** is the heart of the plugin. It declares *where the
values live* in the response, *where the timestamp lives*, and *what the assets
and datapoints should be called* - all as JMESPath expressions.

---

## Installation

The plugin installs into the standard Fledge south plugin path:

```
$FLEDGE_ROOT/python/fledge/plugins/south/restclient/
```

Runtime dependencies (`python/requirements-restclient.txt`):

```
requests>=2.31,<3
jmespath>=1.0,<2
PyYAML>=6.0,<7
```

Install them into Fledge's Python environment:

```bash
pip install -r python/requirements-restclient.txt
```

Then restart Fledge and add a new south service, selecting the **restclient**
plugin.

### Building the package

The `.deb` is built by the shared Fledge plugin workflow, which
`.github/workflows/build-deb.yml` calls on every pull request and on pushes to
`develop`. It packages `python/fledge/plugins/south/restclient/` and installs
the dependencies in `python/requirements-restclient.txt` when the package is
installed.

---

## Quick start

A minimal configuration that polls a flat JSON endpoint such as
`{"temperature": 22.5, "humidity": 60, "ts": "2026-05-15T14:30:00.000Z"}`:

| Setting | Value |
|---|---|
| `url` | `http://localhost:8080/sensors/latest` |
| `responseFormat` | `auto` |
| `mappingSource` | `inline` |
| `readingStructure` | `reading_per_asset` |
| `mappingInline` | *(the YAML below)* |

```yaml
extractions:
  - asset: "weather"
    timestamp: "ts"
    datapoints:
      "temperature": { value: "temperature", cast: "float" }
      "humidity":    { value: "humidity",    cast: "int"   }
```

Produces one reading per poll:

```json
{
  "asset": "weather",
  "timestamp": "2026-05-15 14:30:00.000000+00:00",
  "key": "…uuid…",
  "readings": { "temperature": 22.5, "humidity": 60 }
}
```

> The poll **interval** is supplied by the Fledge south service config, not by
> this plugin. There is intentionally no poll-interval item in the plugin
> configuration.

---

## Configuration reference

Configuration items are grouped in the Fledge UI: **Basic -> Connection ->
Authentication -> Mapping -> Tuning**.

### Basic

| Item | Type | Default | Notes |
|---|---|---|---|
| `assetName` | string | `restclient` | Fallback asset name; used as the asset in `single_reading` mode |

### Connection

| Item | Type | Default | Notes |
|---|---|---|---|
| `url` | string | `http://localhost/` | Full URL including any query string. **Mandatory.** |
| `timeout` | integer | `5000` | Request timeout, in **milliseconds** |
| `verifyTls` | boolean | `true` | Disable only for self-signed dev environments |
| `extraHeaders` | JSON | `{}` | Free-form `{ "Header-Name": "value" }` map added to every request |

### Authentication

| Item | Type | Default | Notes |
|---|---|---|---|
| `authType` | enum | `none` | One of `none`, `basic`, `bearer`, `apiKey` |
| `username` | string | `""` | Used when `authType=basic` |
| `password` | password | `""` | Used when `authType=basic` |
| `bearerToken` | password | `""` | Sent as `Authorization: Bearer <token>` when `authType=bearer` |
| `apiKeyHeader` | string | `X-API-Key` | Header name when `authType=apiKey` |
| `apiKeyValue` | password | `""` | Header value when `authType=apiKey` |

The irrelevant authentication fields are hidden automatically based on
`authType`.

Credentials are never written to the log. The configured `url` is logged with
any `user:password@` part removed and the values of query parameters whose
names look like secrets (token, key, secret, password, signature, auth, code,
session) replaced by `***`. Prefer the `bearer` or `apiKey` header options to
putting a secret in the URL.

### Mapping

| Item | Type | Default | Notes |
|---|---|---|---|
| `responseFormat` | enum | `auto` | `auto`, `odata_v4`, `raw_array`, `raw_object` |
| `mappingSource` | enum | `inline` | `inline` (use `mappingInline`) or `file` (use `mappingFile`) |
| `mappingInline` | JSON | `{}` | The mapping document as a YAML/JSON string |
| `mappingFile` | string | `""` | Filesystem path to a `.yaml`/`.json` mapping document |
| `readingStructure` | enum | `reading_per_asset` | `reading_per_asset`, `reading_per_tag`, `single_reading` |
| `tagsToRead` | JSON | `{"tags":[]}` | Read tag map. Empty = emit every flattened key. Ignored when the mapping already defines `tags`. |

### Tuning

| Item | Type | Default | Notes |
|---|---|---|---|
| `errorHandling` | enum | `partial` | `partial` = emit what extracted, dropping only failing datapoints. `strict` is reserved (see note below) |
| `followPagination` | boolean | `true` | Follow OData `@odata.nextLink` up to `maxPages` |
| `maxPages` | integer | `10` | Hard ceiling on pages followed per poll |
| `maxResponseBytes` | integer | `10485760` | Per-page response size cap (10 MiB) guarding against runaway responses |

---

## The mapping document

A mapping document is a YAML **or** JSON object (YAML is shown throughout this
README because it is friendlier to read; both parse identically). It has two
top-level sections:

```yaml
source:
  records: "value"          # JMESPath to the array of records inside the response.
                            # Optional. Defaults to "@" (the whole payload).
                            # OData v4 responses implicitly use "value".

extractions:                # A list of rules. EACH rule is applied to EACH record.
  - asset: "..."            # Asset name - literal or {template}. Required.
    timestamp: "..."        # JMESPath to the timestamp. Required (unless every
                            # datapoint supplies its own timestamp).
    datapoints:             # Map of datapoint-name -> spec. Required unless
      "datapoint_name":     #   `from` is present; non-empty when present.
        value: "..."        # JMESPath to the value. Required.
        cast: "float"       # Optional: int | float | string | bool.
        timestamp: "..."    # Optional: overrides the rule-level timestamp.
    for_each: "..."         # Optional: see "Iteration" below.
    from: "..."             # Optional: see "Pass-through" below. JMESPath to an
                            #   object; emits one datapoint per key.
    rename: {...}           # Optional, `from` only: source key -> emitted name.
    cast: {...}             # Optional, `from` only: source key -> cast type.
    exclude: [...]          # Optional, `from` only: source keys to drop.

functions: |                # Optional: Python source. Each top-level def
                            #   becomes a JMESPath function (see below).
functionsFile: "..."        # Optional: path to a .py file, same rules.
```

How records are chosen:

1. `source.records` (a JMESPath expression) is evaluated against the response.
2. If it yields a list, each element is a **record**.
3. If it yields a single object, that object is treated as one record.
4. Every rule in `extractions` runs against every record, in order, producing
   stable, deterministic output.

The mapping is **validated at `plugin_init`**. A missing file, malformed
YAML/JSON, an empty `extractions` list, a datapoint with no `value`, an
unknown `cast`, or a rule/datapoint with no timestamp anywhere is a hard
configuration error - the plugin will not start with a broken mapping.

---

## JMESPath: what it is and how this plugin uses it

[**JMESPath**](https://jmespath.org/) (pronounced *"James path"*) is a query
language for JSON, standardized at <https://jmespath.org/specification.html>.
If you have used `aws --query`, you have already written JMESPath - the AWS CLI
uses it for its `--query` flag, which is why it is familiar to many operations
engineers.

A JMESPath expression takes a JSON value as input and returns a JSON value as
output. A few examples against `{"a": {"b": [1, 2, 3]}}`:

| Expression | Result |
|---|---|
| `a.b` | `[1, 2, 3]` |
| `a.b[0]` | `1` |
| `a.b[-1]` | `3` |
| `length(a.b)` | `3` |

This plugin uses JMESPath **everywhere a value is read from the response**:

- `source.records` - locate the array of records.
- each datapoint's `value` - extract the measurement.
- each `timestamp` - extract the time.
- the contents of `{...}` templates in asset/datapoint names.
- `for_each` - locate the array to iterate.

JMESPath was chosen over JSONPath because of its first-class object/array
semantics, its mature, well-maintained Python implementation, and its broad
familiarity.

### JMESPath playground

The public playground at <https://jmespath.org/> and the bundled `jp.py`
CLI evaluate **standard** JMESPath only. They cannot run this plugin's
`from_json()`, `to_array()`, `flatten()`, or `$item` / `$parent` rewrite.

Use the local playground in [`playground/`](playground/) to test a pasted
REST response with the same engine the plugin uses (including completions
that walk into `from_json()` results):

```bash
cd playground
uv run uvicorn app:app --reload --port 8765
```

Then open <http://127.0.0.1:8765>. The Expression tab evaluates a single
query; the Mapping tab runs a full YAML mapping document through
`extraction.extract()`. See [`playground/README.md`](playground/README.md).

The `jmespath` package still ships `jp.py` for vanilla expressions:

```bash
echo '{"value":[{"LastUpdated":"2026-04-16T10:31:33.451Z"}]}' | jp.py 'value[0].LastUpdated'
```

### Custom functions: `from_json()`, `to_array()` and `flatten()`

Standard JMESPath cannot reach into a string that *contains* JSON, cannot
iterate the key/value pairs of an object, and cannot walk a nested tree into
one joined path per leaf. This plugin registers custom functions to close
those gaps. They are available anywhere a JMESPath expression is accepted.

#### `from_json(@)` - parse a stringified-JSON field

Many industrial/.NET REST services embed a JSON document as a **string** inside
a larger JSON response. The anchor use case is an OData batch-history service
whose `Data` field looks like this:

```json
"Data": "{\"M01\":{\"Import\":20.14,\"Export\":0.12,\"Net\":20.02}}"
```

`from_json()` parses that inner string so you can walk into it:

```
from_json(Data).M01.Import      ->  20.14
```

If the field is `null`, an empty string, or not valid JSON, `from_json()`
returns `null` and the affected datapoint is simply dropped (no crash).

#### `to_array(@)` - turn an object into iterable key/value pairs

This is the same idea as `to_entries` in `jq`. Given an object, it returns a
list of `{ "key": ..., "value": ... }` objects so map-shaped data can be
iterated with `for_each`:

```
to_array(from_json(Data))
```

against the `Data` above yields:

```json
[
  { "key": "M01", "value": { "Import": 20.14, "Export": 0.12, "Net": 20.02 } }
]
```

`to_array(null)` returns `[]`.

#### `flatten(@[, identity_keys[, separator]])` - one joined path per leaf

`from` only expands the **top level** of an object. A site tree
(`Lines[]` -> `Equipment[]` -> `Readings` / `Status`) therefore needs either
a rule per object, or a flatten. `flatten()` walks every leaf and
returns a flat object of joined-path keys:

```
flatten(from_json(Data))
```

against the site snapshot uses array indexes (separator defaults to `_`):

```
Lines_0_Equipment_0_Readings_Level  ->  76.412
```

The optional second argument is a list of **identity field names**. Whenever
an object has one of those fields, its value becomes the path segment for
that object, the field itself is not emitted as a leaf, and the container
array name (`Lines`, `Equipment`) is dropped. That is how you choose
`EquipmentId` as the identifier instead of `0`:

```
flatten(from_json(Data), ['SiteId', 'LineId', 'EquipmentId'])
```

JSON-style double quotes (`["SiteId"]`) are rewritten to the same
string-literal list. Without that rewrite JMESPath treats `["SiteId"]`
as a field lookup on the document root (the OData envelope has no
`SiteId`), so flatten would silently do a structural walk.

```
SITE-A_Line1_Tank01_Readings_Level        ->  76.412
SITE-A_Line1_Pump01_Readings_FlowRate     ->  13.402
```

Pass a third argument to change the separator (`'.'`, `'-'`, …):

```
flatten(from_json(Data), ['SiteId', 'LineId', 'EquipmentId'], '.')
```

Identity keys are a per-mapping choice. If a given object does not carry
any of them, `flatten` falls back to the object key or a numeric index, so
every leaf still gets a stable path. Pair with `from` to emit each path as a
datapoint:

```yaml
extractions:
  - asset: "site"
    timestamp: "LastUpdated"
    from: "flatten(from_json(Data), ['SiteId', 'LineId', 'EquipmentId'])"
```

`flatten(null)` returns `{}`.

### Read tag map

`flatten()` + `from` is the catalog: every leaf becomes a datapoint on
the rule's asset, named by its joined path. The **read tag map** is the
allowlist that picks which of those tags to ingest and
which Fledge `(asset, datapoint)` each one becomes.

```yaml
tags:
  - name: "SITE-A_Line1_Tank01_Readings_Level"
    asset: "tank-01"
    datapoint: "level"
  - name: "SITE-A_Line1_Tank01_Readings_Temperature"
    asset: "tank-01"
    datapoint: "temperature"

extractions:
  - asset: "unmapped"
    timestamp: "LastUpdated"
    from: "flatten(from_json(Data), ['SiteId', 'LineId', 'EquipmentId'])"
```

It uses the same JSON shape as the fledge-south-ethernetip read map: `name`
is the flattened source key,
`asset` / `datapoint` are where it is stored. `type` and `program` are
accepted and ignored. A listed name that is missing this poll is skipped.
An empty map leaves `from` unchanged.

Put the list in the mapping document (`tags:` at the top, or on one
`from` rule) or paste it into the plugin **Tags to read** config item.
The mapping wins if both are set. See
[`examples/site-snapshot-tag-map.yaml`](examples/site-snapshot-tag-map.yaml).


### Type hints for the OMF north plugin

Fledge's OMF north plugin decides a datapoint's type from the first value it
sees. REST sources write whole numbers without a decimal point, so an analog
value that reads exactly `0` arrives as an integer, and later fractional
values are rejected. A hint fixes the type up front.

Hint a tag in the tag map with `omfHint`, or a datapoint name once for every
asset and rule with a top-level `omfHints`. A tag's own hint wins:

```yaml
omfHints:
  flow_rate: {number: float64}

tags:
  - name: "SITE-A_Line1_Pump01_Readings_FlowRate"
    asset: "SITE-A-Pump01"
    datapoint: "flow_rate"
    omfHint: {number: float64}
```

Each reading holding a hinted datapoint gets an `OMFHint` datapoint in the
per-datapoint form, which the OMF north plugin reads and does not send on:

```json
{"datapoint":[{"name":"flow_rate","number":"float64"}]}
```

Values are not changed: `0` stays `0` in Fledge. Any datapoint hint key
passes through (`number`, `integer`, `tagName`, `uom`, `minimum`, `maximum`,
...); `name` is reserved. A hint only shapes types that have not already been
created downstream. A type already registered stays until it is recreated.

Anything in the north pipeline that writes `OMFHint` must merge into the
existing value rather than replace it, or these hints are lost.

### User-defined functions

The plugin functions cover the common OData cases. When a site needs a
one-off transform - a slug, a unit conversion, a vendor-specific decode -
you can register extra JMESPath functions by putting ordinary Python
`def`s in the mapping document. They are compiled at `plugin_init` onto
the same engine as `from_json()` / `flatten()`, so they work in `value`,
`timestamp`, `from`, `for_each`, and `{...}` name templates.

```yaml
functions: |
  def slug(value):
      if value is None:
          return None
      return re.sub(r"[^A-Za-z0-9]+", "-", str(value)).strip("-").lower()

  def f_to_c(f):
      return (float(f) - 32.0) * 5.0 / 9.0

extractions:
  - asset: "sensor-{slug(name)}"
    timestamp: "ts"
    datapoints:
      temp_c: { value: "f_to_c(temp_f)", cast: "float" }
```

Against `{"name": "LINE A", "temp_f": 86, ...}` that emits asset
`sensor-line-a` with `temp_c = 30.0`.

Rules:

- Only **top-level** `def` names are exported. Helpers starting with `_`
  stay private.
- Names cannot collide with plugin or standard JMESPath functions
  (`from_json`, `flatten`, `length`, …).
- Every parameter is required - no defaults, `*args`, or `**kwargs`.
  Accept `null` yourself if the field might be missing.
- `json`, `re`, `math`, and `datetime` are already in scope. Other
  imports (`os`, `subprocess`, …) are rejected.
- A syntax error, reserved name, or failed import is a **hard startup
  error**. A function that raises at poll time drops just that datapoint.

For a longer module use `functionsFile: /path/to/funcs.py` (relative paths
are resolved against the mapping file). This is a restricted exec
environment, not a security sandbox - treat mapping documents as trusted
configuration, the same way Fledge treats a `python35` filter script.

### Template substitution in names

Asset names and datapoint names are **templated strings**. Any `{...}` segment
is treated as a JMESPath expression, evaluated against the current record (or
the current iteration item), and substituted in:

```yaml
extractions:
  - asset: "device-{name}"          # {name} -> the record's "name" field
    timestamp: "ts"
    datapoints:
      "metrics.a": { value: "metrics.a", cast: "float" }
```

Given a record `{"name": "d1", ...}`, the asset becomes `device-d1`. If a
template segment resolves to `null`, that asset (or datapoint) is dropped
rather than producing a literal `device-None`.

This is what lets one mapping cover many lines, devices or meters without
duplication.

### Iteration with `for_each`, `$item` and `$parent`

When a single record contains *many* similar sub-structures (e.g. one OData
`Run` carrying a dozen meters), writing one rule per sub-structure is tedious.
`for_each` iterates instead:

```yaml
extractions:
  - for_each: "to_array(from_json(Data))"   # iterate each meter entry
    asset: "EnergyMeter-{$parent.LineId}"
    timestamp: "$parent.LastUpdated"
    datapoints:
      "energy.{$item.key}.import": { value: "$item.value.Import", cast: "float" }
      "energy.{$item.key}.net":    { value: "$item.value.Net",    cast: "float" }
      "energy.{$item.key}.export": { value: "$item.value.Export", cast: "float" }
```

Inside a `for_each` rule, two context variables are available:

- **`$item`** - the current element of the iterated array. Combined with
  `to_array(...)`, `$item.key` is the object key (e.g. `M01`) and
  `$item.value` is its value.
- **`$parent`** - the enclosing record (the OData `Run`), so you can still
  reach `$parent.LastUpdated`, `$parent.LineId`, etc.

`$item` and `$parent` work both in `value`/`timestamp` expressions and inside
`{...}` name templates.

Note the limitation: `for_each` nests **one level deep**. A single rule cannot
iterate lines *and* iterate each line's fields while keeping the line number in
scope - JMESPath projections cannot close over an outer context. Use `from`
(next) for that shape.

### Pass-through with `from`, `rename`, `cast` and `exclude`

The explicit `datapoints` form is right when each datapoint needs its own path,
cast, or timestamp. It becomes pure boilerplate when the source object already
holds the fields under the names you want, and hopeless when you want *every*
field including ones that do not exist yet.

`from` points at an object and emits **one datapoint per key**:

```yaml
extractions:
  - asset: "station-{readings.Station}"
    timestamp: "readings.Timestamp"
    from: "readings"          # emit every key under `readings`
    rename:                   # source key -> emitted name (only what changes)
      SiteCode: Site
      ProductCode: Product
    cast:                     # source key -> cast; "*" is the default
      NetWeight: float
    exclude:                  # source keys to drop
      - Station
      - Timestamp
```

Because keys are discovered **from the data**, a field that is absent from a
given record simply produces no datapoint that cycle - no error - and a field
that appears later is picked up with no mapping change. That is the key
behavioural difference from the static explicit form.

Rules to keep in mind:

- `rename`, `cast`, and `exclude` all key on the **original source key**, never
  on the renamed output name. Mental model: *"here are the source fields -
  rename these, cast these, drop these."*
- `exclude` is applied first, then `rename`, then `cast`.
- `cast: {"*": float}` sets a default for every key not listed individually.
- `from` walks only the **top level** of the target object. A value that is
  itself an object is emitted as a nested datapoint value. Reach into nested
  structure with explicit `datapoints` and `from_json()` / dotted paths.
- A rule using `from` **must** declare a rule-level `timestamp` - there is no
  per-key stanza to hold one.
- `rename`/`cast`/`exclude` on a rule with no `from` is a validation error, not
  a silent no-op.

A rule may carry **both** `from` and `datapoints`. The `from`-derived
datapoints are emitted first, then the explicit ones, so an explicit datapoint
wins on a name collision. That is the intended escape hatch: bulk-emit with
`from`, then override the handful of fields that need bespoke paths or casts.

Several rules may target the **same asset** - they merge into one reading,
because the emission layer groups on `(asset, timestamp)`. This is how you
flatten sibling objects into a single asset:

```yaml
extractions:
  - for_each: &equipment "from_json(Data).Lines[].Equipment[]"
    asset: &asset "{from_json($parent.Data).SiteId}-{$item.EquipmentId}"
    timestamp: &ts "$parent.LastUpdated"
    from: "$item.Readings"

  - for_each: *equipment
    asset: *asset
    timestamp: *ts
    from: "$item.Status"      # merges into the same reading as the rule above
```

See `examples/site-snapshot-full-mapping.yaml` for this pattern applied to the
simulated site: 2 rules capturing every reading and status value across 5
assets, picking up new equipment and new values with no mapping change.

---

## Worked examples

### 1. OData batch history - explicit per-meter rules (verbose, easy to read)

Best when you have a small, fixed set of tags and want the mapping to read like
documentation.

**Response** (`value[].Data` is stringified JSON):

```json
{
  "@odata.context": "…/$metadata#History/Runs",
  "value": [
    {
      "LineId": "LINE-A",
      "LastUpdated": "2026-04-16T10:31:33.451Z",
      "isComplete": false,
      "Data": "{\"M01\":{\"Import\":20.14,\"Export\":0.12,\"Net\":20.02},\"M03\":{\"Import\":19.98,\"Export\":0.11,\"Net\":19.87}}"
    }
  ]
}
```

**Mapping:**

```yaml
source:
  records: "value"

extractions:
  - asset: "EnergyMeter-1"
    timestamp: "LastUpdated"
    datapoints:
      "energy.m01.import": { value: "from_json(Data).M01.Import", cast: "float" }
      "energy.m01.net":    { value: "from_json(Data).M01.Net",    cast: "float" }
      "energy.m01.export": { value: "from_json(Data).M01.Export", cast: "float" }
  - asset: "EnergyMeter-2"
    timestamp: "LastUpdated"
    datapoints:
      "energy.m03.import": { value: "from_json(Data).M03.Import", cast: "float" }
      "energy.m03.net":    { value: "from_json(Data).M03.Net",    cast: "float" }
      "energy.m03.export": { value: "from_json(Data).M03.Export", cast: "float" }
```

**Readings** (`reading_per_asset`):

```json
[
  {
    "asset": "EnergyMeter-1",
    "timestamp": "2026-04-16 10:31:33.451000+00:00",
    "readings": { "energy.m01.import": 20.14, "energy.m01.net": 20.02, "energy.m01.export": 0.12 }
  },
  {
    "asset": "EnergyMeter-2",
    "timestamp": "2026-04-16 10:31:33.451000+00:00",
    "readings": { "energy.m03.import": 19.98, "energy.m03.net": 19.87, "energy.m03.export": 0.11 }
  }
]
```

### 2. OData batch history - iterated over all meters (compact, scales automatically)

Same data, one rule that covers any number of meters. With two active runs
(line 1 carrying `M01`, line 2 carrying `M03`):

**Mapping:**

```yaml
source:
  records: "value"

extractions:
  - for_each: "to_array(from_json(Data))"
    asset: "EnergyMeter-{$parent.LineId}"
    timestamp: "$parent.LastUpdated"
    datapoints:
      "energy.{$item.key}.import": { value: "$item.value.Import", cast: "float" }
      "energy.{$item.key}.net":    { value: "$item.value.Net",    cast: "float" }
      "energy.{$item.key}.export": { value: "$item.value.Export", cast: "float" }
```

**Readings:**

```json
[
  {
    "asset": "EnergyMeter-1",
    "timestamp": "2026-04-16 10:31:33.451000+00:00",
    "readings": { "energy.M01.import": 20.14, "energy.M01.net": 20.02, "energy.M01.export": 0.12 }
  },
  {
    "asset": "EnergyMeter-2",
    "timestamp": "2026-04-16 10:31:34.001000+00:00",
    "readings": { "energy.M03.import": 19.98, "energy.M03.net": 19.87, "energy.M03.export": 0.11 }
  }
]
```

The explicit form (Example 1) is the recommended starting pattern; reach for
the iterated form (Example 2) once you have many similar tags.

### 3. Nested array of devices (non-OData object)

**Response:**

```json
{
  "site": "SITE-A",
  "devices": [
    { "name": "d1", "metrics": {"a": 1.5, "b": 2.5}, "ts": "2026-05-15T14:30:00.000Z" },
    { "name": "d2", "metrics": {"a": 3.5, "b": 4.5}, "ts": "2026-05-15T14:30:00.500Z" }
  ]
}
```

**Mapping** - point `source.records` at the inner array and template the asset:

```yaml
source:
  records: "devices"

extractions:
  - asset: "device-{name}"
    timestamp: "ts"
    datapoints:
      "metrics.a": { value: "metrics.a", cast: "float" }
      "metrics.b": { value: "metrics.b", cast: "float" }
```

### 4. Top-level JSON array

When the whole response **is** the array, use `records: "@"` (the JMESPath
identity, meaning "the current value"):

```yaml
source:
  records: "@"

extractions:
  - asset: "sensor-{id}"
    timestamp: "ts"
    datapoints:
      "temp": { value: "temp", cast: "float" }
```

against `[{"id": "s1", "temp": 22.1, "ts": "…"}, {"id": "s2", "temp": 23.4, "ts": "…"}]`
yields one reading per element (`sensor-s1`, `sensor-s2`).

---

## Reading structure modes

The emission layer groups the extracted tuples according to `readingStructure`:

| Mode | Behavior |
|---|---|
| `reading_per_asset` *(default)* | One reading per `(asset, timestamp)` group, with all of that group's datapoints together. The most natural mapping for historian consumers. |
| `reading_per_tag` | One reading per datapoint - each reading carries a single value. Use when downstream filtering needs per-tag granularity. |
| `single_reading` | Exactly one reading per poll. `asset` comes from `assetName`; all extracted datapoints across all records are flattened into one `readings` dict (last-write-wins on name collisions). Uniquify datapoint names via templates when using this mode. |

Every reading also carries a unique `key` (a UUID) as required by the Fledge
2.0 south contract.

---

## Timestamps

Per the Fledge south guidance, **device-provided timestamps are preferred**.

- Each rule (or per-datapoint override) supplies a `timestamp` JMESPath
  expression. The extracted value should be ISO 8601.
- `Z` and numeric offsets (`-07:00`, `+0500`, `-07`) are converted to UTC.
- A timestamp with **no** timezone is assumed to be **UTC**, and a warning is
  logged.
- If no timestamp is configured, the expression yields `null`, or the value
  can't be parsed (a warning is logged), the plugin falls back to the current
  UTC time.
- Emitted timestamps are always UTC with a space separator and exactly six
  fractional digits, e.g. `2026-04-16 10:31:33.451000+00:00`. That is the only
  form Fledge's C++ ingest (`Reading::stringToTimestamp`) parses correctly: it
  reads `%Y-%m-%d %H:%M:%S`, so an ISO `T` separator silently loses the time
  of day (every reading lands at 00:00:00 UTC), and it takes the fraction as a
  raw microsecond count, so a 7-digit fraction overflows. Extra digits are
  truncated.

> **Keep your API server's clock synchronized via NTP.** The plugin trusts the
> timestamps the endpoint reports and does **not** compensate for clock skew.

---

## Error handling

The plugin **never emits stale data**. If a poll fails for any reason,
`plugin_poll` returns an empty list and Fledge simply records no readings for
that interval.

| Failure | Treatment |
|---|---|
| HTTP timeout | Warn, return `[]` |
| HTTP 4xx / 5xx | Warn, return `[]` (retried next poll) |
| Connection refused | Warn with exponential-backoff log suppression (gaps of 1, 2, 4, 8, 16 polls) so logs are not flooded |
| Invalid JSON in response | Warn with the first 200 chars of the body, return `[]` |
| Response exceeds `maxResponseBytes` | Warn, abort that page, return `[]` |
| OData error envelope (`error.code` + `error.message`) | Warn with the code/message, return `[]` - no partial readings |
| Mapping file missing or invalid | **Hard fail** at `plugin_init` - it is a configuration error |
| A datapoint's JMESPath yields `null`, errors, or fails its `cast` | Drop just that datapoint (debug log); keep the rest |
| Same `@odata.nextLink` seen twice | Pagination-loop guard: warn and stop following |

The `errorHandling` item selects the per-datapoint policy:

- **`partial`** *(default)* - drop the failing datapoint, keep the rest of the
  record's readings.
- **`strict`** - reserved for dropping a whole record when any of its
  datapoints fail. The option is present in the config schema but currently
  behaves the same as `partial`; full strict-mode enforcement is a planned
  enhancement.

---

## OData v4 handling

When `responseFormat=odata_v4` - or `auto` and the response *looks* like OData
(it has a `value` array **and** at least one `@odata.*` key) - the acquisition
layer:

1. Treats the `value` array as the implicit `source.records` (your mapping does
   not need to specify it, though it is harmless to write `records: "value"`).
2. Detects `@odata.nextLink`. If `followPagination=true`, it follows the link
   up to `maxPages` times per poll, concatenating the `value` arrays across
   pages into a single record list before extraction. A relative link is
   resolved against the page that returned it. A link to a different scheme,
   host or port than the configured `url` is **not followed** (a warning is
   logged and the pages read so far are returned), so credentials and headers
   are never sent to another origin.
3. Detects OData error envelopes and refuses to emit partial readings.

This makes the plugin a strong fit for the large class of industrial REST APIs
built on .NET / Web API.

---

## Security notes

- Keep `verifyTls` on outside of development.
- Credentials are sent only to the configured origin (see *OData v4 handling*).
  HTTP redirects returned by the server are followed by the underlying HTTP
  library, which removes the `Authorization` header when the host changes but
  does not remove custom headers, so point `url` at the final address and avoid
  endpoints that redirect to other hosts when `extraHeaders` or `apiKey` carry
  secrets.
- Third-party licences are listed in [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES).
- The `.deb` is not built for Debian 11 (bullseye): its Python 3.9 can only
  install an `urllib3` release with unfixed security advisories.

---

## Development & testing

The test suite is the executable specification. Each fixture under
`tests/fixtures/` is a triple:

```
tests/fixtures/payloads/<name>.json    # the raw HTTP response body
tests/fixtures/mappings/<name>.yaml    # the mapping document that handles it
tests/fixtures/expected/<name>.json    # the expected readings (key fields elided)
```

`tests/test_fixtures_roundtrip.py` is parameterized over every fixture: it
feeds the payload through the full poll path with a mocked HTTP response and
asserts the emitted readings match `expected/` (after stripping the
non-deterministic `key` UUIDs).

Run the suite:

```bash
pip install -r requirements-dev.txt
pytest                       # unit + roundtrip tests
pytest -m integration        # fake-server integration tests
pytest --cov                 # coverage report (target ≥ 85%)
```

The [`playground/`](playground/) app is a local website for pasting a
response body and trying JMESPath queries or mapping documents against
the plugin engine (`uv run uvicorn app:app --reload --port 8765` from
that directory).

`simulator/serve_fixtures.py` serves the fixture payloads over HTTP for manual
end-to-end testing against a real `requests` client - everything moves on a
15-second tick, and `site_snapshot` is driven by a stateful tank-and-pump model
(`simulator/process_simulator.py`) rather than by jittering a template. Run it
directly, or in Docker:

```bash
python3 simulator/serve_fixtures.py                          # binds 0.0.0.0:4444
docker compose -f simulator/docker-compose.yml up -d --build # same host, containerized
curl -s http://127.0.0.1:4444/site_snapshot.json | jq .
```

See [`docs/rest-testing.rst`](docs/rest-testing.rst) for the full walkthrough.

---

## License

Apache 2.0. Copyright (c) 2026 Industrial Intelligence Group, LLC.
