RESTful South Plugin
====================

The ``restclient`` south plugin polls HTTP/REST endpoints, including OData v4
services, and turns the JSON responses into Fledge readings. All reshaping is
declared in a **mapping document** of JMESPath expressions, so a deployment
needs configuration only, not code or a chained filter.

Each poll runs three steps:

#. **Acquisition:** one GET request with the configured authentication, TLS
   settings, timeout, response-size cap and OData pagination.
#. **Extraction:** the mapping document is applied to the JSON response to give
   (asset, datapoint, value, timestamp) tuples.
#. **Emission:** the tuples are grouped into Fledge readings according to
   ``readingStructure``.

A poll that fails at any step returns no readings, so Fledge never receives
stale data.

Requirements
------------

* Fledge 3.1 or later.
* The Python packages in ``python/requirements-restclient.txt`` (``requests``
  2.32.4 or later, ``jmespath`` and ``PyYAML``), installed when the package is
  installed.
* The package is not built for Debian 11 (bullseye).

Configuration
-------------

========================== ===================================================================
Setting                    Description
========================== ===================================================================
``assetName``              Fallback asset name
``url``                    Full URL including any query string (mandatory)
``timeout``                Request timeout in milliseconds (5000 by default)
``verifyTls``              Verify the server certificate (on by default)
``extraHeaders``           JSON map of extra request headers
``authType``               ``none``, ``basic``, ``bearer`` or ``apiKey``
``username``, ``password`` Credentials for ``basic``
``bearerToken``            Token for ``bearer``
``apiKeyHeader``           Header name for ``apiKey`` (``X-API-Key`` by default)
``apiKeyValue``            Header value for ``apiKey``
``responseFormat``         ``auto``, ``odata_v4``, ``raw_array`` or ``raw_object``
``mappingSource``          ``inline`` or ``file``
``mappingInline``          The mapping document, as YAML or JSON
``mappingFile``            Path to a ``.yaml`` or ``.json`` mapping document
``readingStructure``       ``reading_per_asset``, ``reading_per_tag`` or ``single_reading``
``tagsToRead``             Read tag map. Empty emits every flattened key
``errorHandling``          ``partial`` emits what extracted and drops failing datapoints
``followPagination``       Follow OData ``@odata.nextLink``
``maxPages``               Maximum pages followed per poll (10 by default)
``maxResponseBytes``       Per-page size cap (10 MiB by default)
========================== ===================================================================

The README in the repository describes the mapping document, JMESPath functions,
tag maps, templates and worked examples in full.

Timestamps
----------

Source timestamps are converted to the form Fledge reads, which is UTC with a
space separator and six fractional digits, for example
``2026-11-24 15:40:08.209751+00:00``. A timestamp without a zone is treated as
UTC, and an unparseable one is replaced by the poll time.

Type hints for the OMF north plugin
-----------------------------------

A tag can carry an ``omfHint`` (or the mapping can carry a top-level
``omfHints`` map keyed by datapoint name) that is attached to the reading as an
``OMFHint`` datapoint. The Fledge OMF north plugin uses it to choose the type of
a datapoint, for example ``float64``. A hint on a tag takes priority over one in
``omfHints``.

OData pagination and security
-----------------------------

With ``followPagination`` on, the plugin follows ``@odata.nextLink`` up to
``maxPages`` times and merges the pages. A relative link is resolved against the
page that returned it. A link to a different scheme, host or port than ``url`` is
not followed, so credentials and headers are only sent to the configured origin.
A warning is logged and the pages read so far are used.

The ``url`` is written to the log with ``user:password@`` removed and the values
of secret-looking query parameters replaced by ``***``. Use the ``bearer`` or
``apiKey`` options instead of placing a secret in the URL, and keep ``verifyTls``
on outside development.

Testing
-------

.. toctree::
   :maxdepth: 2

   rest-testing
