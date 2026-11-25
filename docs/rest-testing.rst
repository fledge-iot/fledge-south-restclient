REST endpoint testing
=====================

This document explains how to bring up a local HTTP host that serves
the plugin's fixtures as simulated live time-series data, and how to
exercise each scenario against it - either by curling the host directly,
by running the integration test suite, or by pointing a Fledge instance
with the ``restclient`` plugin at it.

Two distinct hosts appear in this repo and it's worth keeping them
straight:

* ``simulator/serve_fixtures.py`` - the **simulation host** documented
  here. Serves every fixture under ``tests/fixtures/payloads/`` as live
  data on a 15-second tick: most are templates re-rendered with fresh
  timestamps and jittered numbers, while ``site_snapshot`` is driven by
  the stateful process model in ``simulator/process_simulator.py``. Long-lived,
  bound to ``0.0.0.0:4444``, used for manual testing and as a data source
  for a live Fledge service.
* The ``_FakeServer`` class in
  ``tests/integration/test_fake_server_roundtrip.py`` - an in-process
  Werkzeug server used only by the integration test suite. It returns
  fixture payloads **verbatim** so they can be compared to
  ``tests/fixtures/expected/``. You don't run it directly; pytest
  manages its lifecycle.

Prerequisites
-------------

Install the dev requirements (Flask is what the fake host runs on)::

    pip3 install --user -r requirements-dev.txt

The plugin's own runtime deps (``requests``, ``jmespath``, ``PyYAML``)
should already be installed if you've followed the README.

Option C runs the host in a container instead and needs none of this -
only Docker.

Option A - let pytest manage the host
-------------------------------------

The fastest way to exercise every scenario end-to-end is to run the
integration suite. It boots the fake server, parameterises over every
fixture under ``tests/fixtures/payloads/``, runs the plugin's full poll
path against it, and compares the emitted readings to
``tests/fixtures/expected/``::

    cd fledge-south-restclient
    pytest -m integration -v

Run a single scenario by name::

    pytest -m integration -v -k batch_runs_single_active

The suite tears the server down when the run completes; nothing is left
listening.

Option B - keep the host running for manual poking
--------------------------------------------------

When you want to ``curl`` the endpoints, point a live Fledge service at
them, or attach a debugger, run the simulation host as a long-lived
process. The repo ships ``simulator/serve_fixtures.py``::

    python3 simulator/serve_fixtures.py

It binds to ``0.0.0.0:4444`` so it's reachable from other hosts on the
network (use ``http://<this-host-ip>:4444/...`` from a remote Fledge
instance or a browser, or ``http://127.0.0.1:4444/...`` from the same host).
Each fixture is reachable at either ``/<name>`` or ``/<name>.json`` -
the bare-name form is nicer to type in a browser, the ``.json`` form
matches what the integration test fixtures use. ``GET /`` returns an
index listing the available fixtures. ``Ctrl-C`` to stop.

.. note::

   Binding to ``0.0.0.0`` exposes the host on every interface, including
   any public ones. The fake server has no auth - only run it on trusted
   networks, or firewall the port.

Simulated time-series behavior
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The host advances on a fixed **15-second tick**
(``process_simulator.TICK_SECONDS``). Every request landing inside the same
tick gets a byte-identical response; the data only moves when the tick
rolls over. A south service polling every 5 seconds therefore sees the
same values three times in a row - exactly what a real snapshot endpoint
with a poll rate does, and a useful thing to have in front of you when
tuning a south service's poll interval.

Most fixtures are loaded once at startup as **templates** and re-rendered
per tick:

* **Timestamps** - every ISO 8601 string in the document is shifted so
  the latest one lines up with the current tick boundary. Intra-record
  offsets are preserved (e.g. ``StartTime`` stays ~16 minutes before
  ``LastUpdated`` in the batch-history fixtures).
* **Numeric values** - floats jitter ±2 % of their template value
  (minimum ±0.05); integers jitter ±1. Booleans, IDs (UUIDs, line
  names), units, and other strings are left alone.
* **Stringified JSON fields** - the OData ``Data`` field is parsed,
  recursively transformed, and re-serialized in the same compact form.

``site_snapshot`` is the exception: it is produced by the **process
model** in ``simulator/process_simulator.py``, not by jittering a template.
See the next section.

Two effects to be aware of:

1. The static ``tests/fixtures/expected/`` files **do not match** the
   simulated output. Use this host for manual / interactive testing
   only; the parameterized roundtrip test uses its own in-process
   fixture server that returns the payloads verbatim.
2. For the template-driven fixtures, repeated polls produce a small
   random walk, not a smooth one. Good enough for verifying that the
   plugin is acquiring, mapping, and emitting on every cycle - not a
   substitute for a real process simulator.

The process model behind ``site_snapshot``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``simulator/process_simulator.py`` models a small, invented site as a
telemetry snapshot endpoint might expose it: **one snapshot row** whose
``Data`` field is a *stringified* JSON document holding the whole site,
``Lines[] -> Equipment[]``, each piece of equipment with ``Readings`` and
``Status`` objects. Each line is a tank filled by a pump and drained by a
steady outflow:

* the pump starts when the tank level drops below 35 % and stops above
  80 %; while it runs it reports flow, discharge pressure and motor
  current, and ``TotalVolume`` accumulates
* the tank reports its level and a temperature that follows a daily cycle,
  plus high and low level alarms
* ``Line1`` also has an outlet valve whose position wanders and sets the
  outflow; ``Line2`` has no valve, a deliberate asymmetry that exercises
  the mapping's key discovery

Every name in it is invented.

Because the model is stateful, polling it repeatedly shows process
behavior rather than noise: levels rise and fall, pumps start and stop,
and totals only ever climb.

Its mapping is the shipped example,
``examples/site-snapshot-full-mapping.yaml`` - copied into
``tests/fixtures/mappings/site_snapshot.yaml`` and pinned there by
``test_example_mapping_matches_fixture_mapping``. So this endpoint is the
one to point a south service at when you want to exercise that example
against live-looking data.

The model is deterministic - same ``seed`` + same ``start_tick`` + same
tick sequence produces the same payload. That is what lets
``tests/fixtures/payloads/site_snapshot.json`` be a checked-in snapshot
of the model rather than a hand-written file; ``tests/test_process_simulator.py``
asserts the two agree. If you change the model, regenerate both the
payload and its expected readings::

    python3 -c "import sys, json; sys.path.insert(0, 'simulator'); \
        import process_simulator as p; \
        print(json.dumps(p.fixture_snapshot(), indent=2))" \
        > tests/fixtures/payloads/site_snapshot.json

Then re-derive ``tests/fixtures/expected/site_snapshot.json`` by running
the harness in "Running the plugin against the fake host" below against
that payload and saving the readings with their ``key`` fields stripped.

.. note::

   ``LastUpdated`` carries .NET's 7-digit fractional second
   (``2026-08-04T13:05:45.7308710Z``), as .NET-backed OData endpoints do. The
   plugin converts that to the form Fledge's C++ ingest parses: UTC, a space
   separator, and six fractional digits
   (``2026-08-04 13:05:45.730871+00:00``).

Option C - run the host in Docker
---------------------------------

The same host, containerized, for when you want it running as a service
rather than as a terminal you have to keep open - or on a box where you'd
rather not install Flask::

    cd simulator
    docker compose up -d --build
    curl -s http://127.0.0.1:4444/site_snapshot.json | jq .
    docker compose logs -f
    docker compose down

Or, without leaving the repo root::

    docker compose -f simulator/docker-compose.yml up -d --build

``simulator/Dockerfile`` builds a ``python:3.12-slim`` image containing
``simulator/`` and ``tests/fixtures/payloads/`` - the simulation host only,
not the plugin - served by gunicorn on port 4444 as an unprivileged user,
with a healthcheck on ``GET /``. ``simulator/docker-compose.yml`` publishes
it and restarts it unless stopped. The **build context is the repo root**,
not ``simulator/``, because the image needs the fixture payloads too.

Two knobs, both optional::

    SIM_PORT=8080 docker compose up -d      # host port; container stays on 4444
    SIM_SEED=1234 docker compose up -d      # pin the process model

``SIM_SEED`` chooses the starting state: the same seed always starts from
the same tank levels and pump states. Unset means the built-in default
seed, so every container start looks the same. (The seed fixes the
starting state, not the whole timeline - evolution from there is tied to
the wall-clock tick, so two hosts started together with the same seed
stay in step, while a restart hours later replays from the same start
along a different path.)

.. note::

   The compose file runs **one** replica and the image runs gunicorn with
   ``--workers 1``, both deliberately. The process model is in-process
   state, so a second worker is a second, unrelated model - a poller
   would flip between two versions of reality on alternate requests.
   Concurrency comes from threads.

To edit payload templates without rebuilding, uncomment the ``volumes``
block in ``simulator/docker-compose.yml`` to bind-mount ``tests/fixtures/payloads/``
(and ``simulator/``) over the baked-in copies.

From another host on the network, use the host's IP:
``http://<this-host-ip>:4444/...`` - and remember the same caveat as
Option B: no auth, so keep it on a trusted network or firewall the port.

The scenarios
-------------

Each row below is a fixture triple under ``tests/fixtures/`` -
``payloads/<name>.json``, ``mappings/<name>.yaml``, and
``expected/<name>.json``. Append ``.json`` to the name for the URL on the
fake host.

.. list-table::
   :header-rows: 1
   :widths: 28 20 52

   * - Fixture
     - Response format
     - What it exercises
   * - ``batch_runs_single_active``
     - OData v4
     - Anchor case. One active run, both meters in the stringified
       ``Data`` field. Verifies ``from_json()`` + per-asset emission.
   * - ``batch_runs_dual_active``
     - OData v4
     - Two active runs (line 1 and line 2), each with one meter. Verifies
       multi-record extraction and per-asset grouping.
   * - ``batch_runs_paginated_page1``
     - OData v4
     - First page with ``@odata.nextLink`` set. The link points at
       ``odata.example.local:4444`` - see "Pagination" below.
   * - ``batch_runs_paginated_page2``
     - OData v4
     - Second page completing the paginated set.
   * - ``batch_runs_empty``
     - OData v4
     - ``{"value": []}``. Plugin must emit ``[]`` cleanly, no errors.
   * - ``batch_runs_stale_data``
     - OData v4 (degenerate)
     - ``Data`` is null / empty. Verifies the partial-error path drops
       the offending datapoints without poisoning the whole reading.
   * - ``site_snapshot``
     - OData v4 (snapshot)
     - One snapshot row whose stringified ``Data`` holds a whole site:
       ``Lines[] -> Equipment[]``. Its mapping is
       ``examples/site-snapshot-full-mapping.yaml``, so this triple is the
       regression test for that shipped example - 2 rules, 5 assets.
       Exercises ``from_json`` on a nested document, a flatten projection
       in ``for_each``, ``from`` key discovery over asymmetric lines, and
       several rules merging into one asset.
   * - ``odata_error``
     - OData v4 error envelope
     - ``{"error": {...}}``. Plugin must log a warning and return ``[]``.
   * - ``flat_object``
     - raw_object
     - Bare ``{"temperature": ..., "humidity": ..., "ts": ...}``. Tests
       the no-records-array path.
   * - ``flat_array_sensors``
     - raw_array
     - Top-level array of sensor objects. Tests record iteration when
       the response is an array rather than an envelope.
   * - ``nested_devices``
     - raw_object
     - Top-level object with a nested ``devices`` array containing
       per-device metrics. Exercises ``source.records`` JMESPath +
       ``for_each``.
   * - ``flatten_hierarchy``
     - OData v4
     - Compact site tree (Tank01 on Line1, Pump02 on Line2). Verifies
       ``flatten(from_json(Data), identity_keys)`` + ``from`` emits one
       joined datapoint per leaf (``SITE-A_Line1_Tank01_Readings_Level``).
   * - ``flatten_tag_map``
     - OData v4
     - Same site tree. A read tag map (``tags``) routes three flattened
       keys onto ``tank-01`` and ``pump-02``.
   * - ``user_functions``
     - raw_object
     - Flat sensor object. Verifies mapping ``functions:`` - ``slug()``
       in the asset template and ``f_to_c()`` on the value.

Curling the scenarios
---------------------

With Option B running::

    curl -s http://127.0.0.1:4444/batch_runs_single_active.json | jq .
    curl -s http://127.0.0.1:4444/flat_object.json | jq .
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4444/nope.json
    # 404

Call the same URL twice in quick succession to see the simulation in
action - the timestamp moves forward and numeric values walk slightly,
while string IDs, units, and booleans stay put::

    for i in 1 2 3; do curl -s http://127.0.0.1:4444/flat_object.json; echo; done

Running the plugin against the fake host
----------------------------------------

A minimal harness that mirrors what ``plugin_poll`` does inside Fledge
- useful for iterating on a mapping document without restarting the
south service::

    import copy, json
    from python.fledge.plugins.south.restclient import restclient as plugin

    cfg = copy.deepcopy(plugin._DEFAULT_CONFIG)
    cfg_values = {k: {"value": v["default"]} for k, v in cfg.items()}
    cfg_values["url"]["value"] = "http://127.0.0.1:4444/batch_runs_single_active.json"
    cfg_values["mappingSource"]["value"] = "inline"
    cfg_values["mappingInline"]["value"] = open(
        "tests/fixtures/mappings/batch_runs_single_active.yaml"
    ).read()
    cfg_values["followPagination"]["value"] = "false"

    handle = plugin.plugin_init(cfg_values)
    try:
        print(json.dumps(plugin.plugin_poll(handle), indent=2, default=str))
    finally:
        plugin.plugin_shutdown(handle)

Swap the fixture name in two places (URL + mapping path) to switch
scenarios.

Pointing a real Fledge south service at the host
------------------------------------------------

For an end-to-end smoke test through the live Fledge instance on this
machine:

1. Start the fake host (Option B above).
2. In the Fledge UI, add a new south service using the ``restclient``
   plugin.
3. Configure the **Connection** group:

   - ``url``: ``http://127.0.0.1:4444/batch_runs_single_active.json``
   - ``timeout``: ``5000``
   - ``verifyTls``: leave on (HTTP, no TLS in play)

4. Configure the **Mapping** group:

   - ``mappingSource``: ``inline``
   - ``mappingInline``: paste the contents of
     ``tests/fixtures/mappings/batch_runs_single_active.yaml``
   - ``readingStructure``: ``reading_per_asset``

5. Save and watch the south service's readings count climb in the UI,
   or tail readings via Fledge's REST API::

       curl -s http://localhost:8081/fledge/asset | jq .
       curl -s 'http://localhost:8081/fledge/asset/EnergyMeter-1?limit=5' | jq .

Repeat with each fixture URL to walk through the scenarios.

Pagination
----------

The page-1 fixture's ``@odata.nextLink`` points at
``http://odata.example.local:4444/...`` - a non-resolvable host on
purpose, so unit tests never need DNS. Two ways to exercise pagination
against the fake host:

- **In tests**: the integration suite sets ``followPagination=false``,
  so the page-1 fixture is treated as a single response. The unit suite
  drives multi-page behavior by mocking ``requests`` with ``responses``
  - see ``tests/test_acquisition.py``.
- **Manually**: edit ``batch_runs_paginated_page1.json`` to point its
  ``@odata.nextLink`` at ``http://127.0.0.1:4444/batch_runs_paginated_page2.json``,
  enable ``followPagination`` on the plugin config, and poll. Revert the
  fixture change when done - the integration suite's expected output
  assumes the unresolved URL.

Authentication
--------------

The fake host doesn't enforce auth, but the plugin's auth code paths can
still be exercised against it. Set ``authType`` and the matching
credential field; the plugin will attach the header / basic-auth tuple
to the outgoing ``requests`` call regardless of whether the server
checks. Confirm the header arrived by adding a ``before_request`` hook
to the Flask app and ``print(dict(flask.request.headers))``.

Troubleshooting
---------------

- ``ConnectionRefusedError`` from the plugin - the fake host isn't
  running, or it's bound to a different port. Confirm with
  ``curl http://127.0.0.1:4444/flat_object.json``.
- ``ModuleNotFoundError: flask`` - dev deps not installed; see
  Prerequisites.
- Plugin returns ``[]`` and logs a JMESPath error - the mapping
  references a field that isn't in the payload. Re-check by curling the
  URL and reading the mapping side-by-side; the expected output file
  shows what *should* survive extraction.
