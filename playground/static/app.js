const DEFAULT_JSON = `{
  "@odata.context": "http://odata.example.local:4444/BatchHistory/$metadata#History/Runs",
  "value": [
    {
      "RunId": "2c6e8a4f-1234-5678-9abc-def012345678",
      "LineId": "LINE-A",
      "RecipeName": "PRODUCT-A",
      "StartTime": "2026-04-16T10:15:00.000Z",
      "EndTime": null,
      "LastUpdated": "2026-04-16T10:31:33.451Z",
      "isComplete": false,
      "Data": "{\\"M01\\":{\\"Import\\":20.14,\\"Export\\":0.12,\\"Net\\":20.02,\\"Units\\":\\"kWh\\",\\"Valid\\":true},\\"M03\\":{\\"Import\\":19.98,\\"Export\\":0.11,\\"Net\\":19.87,\\"Units\\":\\"kWh\\",\\"Valid\\":true}}"
    }
  ]
}`;

const DEFAULT_QUERY = "from_json(value[0].Data).M01.Import";

const DEFAULT_MAPPING = `source:
  records: "value"

extractions:
  - asset: "EnergyMeter-1"
    timestamp: "LastUpdated"
    datapoints:
      "energy.m01.import": { value: "from_json(Data).M01.Import", cast: "float" }
      "energy.m01.net":    { value: "from_json(Data).M01.Net",    cast: "float" }
`;

const DEFAULT_FUNCTIONS = `# Optional. Top-level defs become JMESPath functions.
# json, re, math, and datetime are already in scope.
#
# def slug(value):
#     if value is None:
#         return None
#     return re.sub(r"[^A-Za-z0-9]+", "-", str(value)).strip("-").lower()
`;

const jsonStatus = document.getElementById("json-status");
const exprError = document.getElementById("expr-error");
const exprResult = document.getElementById("expr-result");
const mapError = document.getElementById("map-error");
const tupleTable = document.querySelector("#tuple-table tbody");
const tupleCount = document.getElementById("tuple-count");
const bindIteration = document.getElementById("bind-iteration");
const exampleSelect = document.getElementById("example-select");
const fnStatus = document.getElementById("fn-status");
const fnError = document.getElementById("fn-error");
const functionsDetails = document.getElementById("functions-details");

let examples = [];
let jsonEditor;
let queryEditor;
let mappingEditor;
let functionsEditor;
let activeTab = "expression";
let debounceTimer;

function debounce(fn, ms) {
  return (...args) => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => fn(...args), ms);
  };
}

const scheduleRun = debounce(runActive, 250);

function parsePayload() {
  const text = jsonEditor.getValue();
  try {
    const payload = JSON.parse(text);
    jsonStatus.textContent = "valid JSON";
    jsonStatus.className = "status ok";
    return { payload, error: null };
  } catch (err) {
    jsonStatus.textContent = err.message;
    jsonStatus.className = "status bad";
    return { payload: null, error: err.message };
  }
}

async function runExpression() {
  const { payload, error } = parsePayload();
  if (error) {
    showError(exprError, `JSON: ${error}`);
    exprResult.textContent = "";
    return;
  }
  const query = queryEditor.getValue();
  const resp = await fetch("/api/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      json: payload,
      query,
      bind_iteration: bindIteration.checked,
      functions: functionsEditor.getValue(),
    }),
  });
  const data = await resp.json();
  updateFnStatus(data);
  if (data.error) {
    const isFn = String(data.error).startsWith("functions:");
    showError(isFn ? fnError : exprError, data.error);
    if (isFn) hideError(exprError);
    else hideError(fnError);
    exprResult.textContent = "";
    return;
  }
  hideError(exprError);
  hideError(fnError);
  if (!query.trim()) {
    exprResult.textContent = "Type a JMESPath query.";
    return;
  }
  exprResult.textContent = JSON.stringify(data.result, null, 2);
}

async function runMapping() {
  const { payload, error } = parsePayload();
  if (error) {
    showError(mapError, `JSON: ${error}`);
    renderTuples([]);
    return;
  }
  const resp = await fetch("/api/extract", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      json: payload,
      mapping: mappingEditor.getValue(),
      functions: functionsEditor.getValue(),
    }),
  });
  const data = await resp.json();
  updateFnStatus(data);
  if (data.error) {
    const isFn = String(data.error).startsWith("functions:");
    showError(isFn ? fnError : mapError, data.error);
    if (isFn) hideError(mapError);
    else hideError(fnError);
    renderTuples([]);
    return;
  }
  hideError(mapError);
  hideError(fnError);
  renderTuples(data.tuples || []);
}

function runActive() {
  if (activeTab === "mapping") {
    return runMapping();
  }
  return runExpression();
}

function showError(el, message) {
  el.hidden = false;
  el.textContent = message;
}

function hideError(el) {
  el.hidden = true;
  el.textContent = "";
}

function updateFnStatus(data) {
  const names = data.functions || [];
  fnStatus.textContent = names.length ? `registered: ${names.join(", ")}` : "";
  fnStatus.className = names.length ? "status ok" : "status";
}

function renderTuples(tuples) {
  tupleTable.replaceChildren();
  tupleCount.textContent = tuples.length ? `(${tuples.length})` : "";
  for (const row of tuples) {
    const tr = document.createElement("tr");
    for (const key of ["asset", "datapoint", "value", "timestamp"]) {
      const td = document.createElement("td");
      const value = row[key];
      td.textContent = typeof value === "string" ? value : JSON.stringify(value);
      tr.appendChild(td);
    }
    tupleTable.appendChild(tr);
  }
}

async function completeQuery(context) {
  const { payload, error } = parsePayload();
  if (error) return null;
  const query = context.state.doc.toString();
  const cursor = context.pos;
  const resp = await fetch("/api/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      json: payload,
      query,
      cursor,
      bind_iteration: bindIteration.checked,
      functions: functionsEditor.getValue(),
    }),
  });
  const data = await resp.json();
  const options = (data.suggestions || []).map((item) => ({
    label: item.label,
    type: item.type,
    detail: item.detail,
  }));
  if (!options.length) return null;
  return {
    from: data.from ?? cursor,
    options,
  };
}

function createTextareaEditor(mount, value, extraClass) {
  const ta = document.createElement("textarea");
  ta.className = extraClass || "";
  ta.value = value;
  ta.spellcheck = false;
  ta.style.cssText = "width:100%;height:100%;border:0;resize:none;background:transparent;color:inherit;font:13px ui-monospace,monospace;padding:0.6rem;outline:none;";
  mount.appendChild(ta);
  ta.addEventListener("input", scheduleRun);
  return {
    getValue: () => ta.value,
    setValue: (next) => {
      ta.value = next;
    },
  };
}

async function createEditors() {
  try {
    const [
      { basicSetup },
      { EditorView, keymap },
      { EditorState },
      { json },
      { yaml },
      { python },
      { autocompletion },
    ] = await Promise.all([
      import("https://esm.sh/codemirror@6.0.1"),
      import("https://esm.sh/@codemirror/view@6"),
      import("https://esm.sh/@codemirror/state@6"),
      import("https://esm.sh/@codemirror/lang-json@6"),
      import("https://esm.sh/@codemirror/lang-yaml@6"),
      import("https://esm.sh/@codemirror/lang-python@6"),
      import("https://esm.sh/@codemirror/autocomplete@6"),
    ]);

    const theme = EditorView.theme({
      "&": { height: "100%", backgroundColor: "#1e2126" },
      ".cm-content": { caretColor: "#e8eaed" },
      ".cm-gutters": { backgroundColor: "#1e2126", color: "#9aa0a6", border: "none" },
      "&.cm-focused": { outline: "none" },
    }, { dark: true });

    function mountCM(el, doc, extensions) {
      const view = new EditorView({
        state: EditorState.create({
          doc,
          extensions: [
            basicSetup,
            theme,
            EditorView.updateListener.of((update) => {
              if (update.docChanged) scheduleRun();
            }),
            ...extensions,
          ],
        }),
        parent: el,
      });
      return {
        getValue: () => view.state.doc.toString(),
        setValue: (next) => {
          view.dispatch({
            changes: { from: 0, to: view.state.doc.length, insert: next },
          });
        },
      };
    }

    jsonEditor = mountCM(document.getElementById("json-editor"), DEFAULT_JSON, [json()]);
    queryEditor = mountCM(document.getElementById("query-editor"), DEFAULT_QUERY, [
      autocompletion({ override: [completeQuery], activateOnTyping: true }),
      keymap.of([]),
    ]);
    mappingEditor = mountCM(document.getElementById("mapping-editor"), DEFAULT_MAPPING, [yaml()]);
    functionsEditor = mountCM(
      document.getElementById("functions-editor"),
      DEFAULT_FUNCTIONS,
      [python()],
    );
  } catch (err) {
    console.warn("CodeMirror CDN unavailable, falling back to textareas", err);
    jsonEditor = createTextareaEditor(document.getElementById("json-editor"), DEFAULT_JSON);
    queryEditor = createTextareaEditor(document.getElementById("query-editor"), DEFAULT_QUERY);
    mappingEditor = createTextareaEditor(document.getElementById("mapping-editor"), DEFAULT_MAPPING);
    functionsEditor = createTextareaEditor(
      document.getElementById("functions-editor"),
      DEFAULT_FUNCTIONS,
    );
  }
}

function setupTabs() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      activeTab = btn.dataset.tab;
      document.querySelectorAll(".tab").forEach((other) => {
        other.classList.toggle("active", other === btn);
      });
      document.getElementById("panel-expression").hidden = activeTab !== "expression";
      document.getElementById("panel-mapping").hidden = activeTab !== "mapping";
      runActive();
    });
  });
}

async function loadExamples() {
  try {
    const resp = await fetch("/api/examples");
    const data = await resp.json();
    examples = data.examples || [];
  } catch {
    examples = [];
  }
  for (const example of examples) {
    const opt = document.createElement("option");
    opt.value = example.name;
    opt.textContent = example.name;
    exampleSelect.appendChild(opt);
  }
  const preferred = examples.find((item) => item.name === "batch_runs_single_active");
  if (preferred) {
    exampleSelect.value = preferred.name;
    applyExample(preferred);
  }
}

function applyExample(example) {
  jsonEditor.setValue(JSON.stringify(example.payload, null, 2));
  if (example.mapping) {
    mappingEditor.setValue(example.mapping);
  }
  if (example.query) {
    queryEditor.setValue(example.query);
  }
  if (example.functions) {
    functionsEditor.setValue(example.functions);
    functionsDetails.open = true;
  } else {
    functionsEditor.setValue(DEFAULT_FUNCTIONS);
    functionsDetails.open = false;
  }
}

exampleSelect.addEventListener("change", () => {
  const example = examples.find((item) => item.name === exampleSelect.value);
  if (example) applyExample(example);
  scheduleRun();
});

bindIteration.addEventListener("change", scheduleRun);

setupTabs();
await createEditors();
await loadExamples();
await runActive();
