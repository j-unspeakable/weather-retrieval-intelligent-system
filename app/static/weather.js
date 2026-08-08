"use strict";

const progressPanel = document.querySelector("#sync-progress");
const progressTitle = document.querySelector("#progress-title");
const progressCount = document.querySelector("#progress-count");
const progressBar = document.querySelector("#progress-bar");
const progressStatus = document.querySelector("#progress-status");
const syncResults = document.querySelector("#sync-results");

function selectedValues(form, name) {
  return [...form.querySelectorAll(`input[name="${name}"]:checked`)].map(
    (input) => input.value,
  );
}

function setFormsDisabled(disabled) {
  document.querySelectorAll("#state-sync-form, #point-sync-form").forEach((form) => {
    form.querySelectorAll("button, input, textarea, select").forEach((control) => {
      control.disabled = disabled;
    });
  });
}

function showProgress(title, total) {
  progressPanel.hidden = false;
  progressTitle.textContent = title;
  progressBar.max = total;
  progressBar.value = 0;
  progressCount.textContent = `0 / ${total}`;
  progressStatus.textContent = "Starting…";
  syncResults.replaceChildren();
  progressPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function appendResult(label, status, detail) {
  const item = document.createElement("li");
  item.className = `sync-result ${status}`;
  const heading = document.createElement("strong");
  heading.textContent = label;
  const message = document.createElement("span");
  message.textContent = detail;
  item.append(heading, message);
  syncResults.append(item);
}

async function responseError(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((error) => error.msg).join("; ");
    }
  } catch (_error) {
    // Fall through to the HTTP status when a proxy returns non-JSON content.
  }
  return `Request failed with HTTP ${response.status}`;
}

async function runSequential(items, title, requestItem, summarize) {
  showProgress(title, items.length);
  setFormsDisabled(true);
  let totalSynced = 0;
  let failures = 0;
  const savedResults = [];

  for (const [index, item] of items.entries()) {
    progressStatus.textContent = `Syncing ${item.label}…`;
    const itemStartedAt = Date.now();
    const elapsedTimer = window.setInterval(() => {
      const elapsedSeconds = Math.floor((Date.now() - itemStartedAt) / 1000);
      progressStatus.textContent = `Syncing ${item.label}… ${elapsedSeconds}s elapsed; upstream requests are still in progress.`;
    }, 1000);
    try {
      const response = await requestItem(item);
      if (!response.ok) throw new Error(await responseError(response));
      const body = await response.json();
      totalSynced += body.synced;
      const detail = summarize(body);
      appendResult(item.label, "complete", detail);
      savedResults.push({ label: item.label, status: "complete", detail });
    } catch (error) {
      failures += 1;
      const detail = error instanceof Error ? error.message : "Unknown sync error";
      appendResult(item.label, "failed", detail);
      savedResults.push({ label: item.label, status: "failed", detail });
    } finally {
      window.clearInterval(elapsedTimer);
    }
    progressBar.value = index + 1;
    progressCount.textContent = `${index + 1} / ${items.length}`;
  }

  const completed = items.length - failures;
  progressStatus.textContent = failures
    ? `Finished: ${completed} completed, ${failures} failed, ${totalSynced} documents synced.`
    : `Complete: ${totalSynced} documents synced successfully.`;
  setFormsDisabled(false);

  if (completed > 0) {
    sessionStorage.setItem(
      "weather-sync-results",
      JSON.stringify({ title, totalSynced, results: savedResults }),
    );
    window.setTimeout(() => {
      window.location.assign(`/weather?synced=${totalSynced}`);
    }, 900);
  }
}

const stateForm = document.querySelector("#state-sync-form");
const stateCheckboxes = [...document.querySelectorAll('input[name="states"]')];
const selectedStateCount = document.querySelector("#selected-state-count");

function updateSelectedStateCount() {
  const count = stateCheckboxes.filter((checkbox) => checkbox.checked).length;
  if (selectedStateCount) {
    selectedStateCount.textContent = `${count} selected`;
  }
}

stateCheckboxes.forEach((checkbox) => {
  checkbox.addEventListener("change", updateSelectedStateCount);
});

document.querySelector("#select-all-states")?.addEventListener("click", () => {
  stateCheckboxes.forEach((checkbox) => {
    checkbox.checked = true;
  });
  updateSelectedStateCount();
});

document.querySelector("#clear-all-states")?.addEventListener("click", () => {
  stateCheckboxes.forEach((checkbox) => {
    checkbox.checked = false;
  });
  updateSelectedStateCount();
});

updateSelectedStateCount();

stateForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  const states = stateCheckboxes.filter((checkbox) => checkbox.checked).map((checkbox) => ({
    value: checkbox.value,
    label: checkbox.closest("label")?.title || checkbox.value,
  }));
  const sourceTypes = selectedValues(stateForm, "state_source_types");
  const stationLimit = Number(stateForm.querySelector("#station-limit").value);

  if (!states.length || !sourceTypes.length) {
    showProgress("State synchronization", 1);
    progressStatus.textContent = "Select at least one state and one source type.";
    return;
  }

  runSequential(
    states,
    "State synchronization",
    (state) => fetch("/weather/sync-state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state: state.value,
        source_types: sourceTypes,
        station_limit: stationLimit,
      }),
    }),
    (body) => {
      const coverage = [];
      if (body.zones_processed) coverage.push(`${body.zones_processed} zones`);
      if (body.stations_processed) coverage.push(`${body.stations_processed} stations`);
      coverage.push(`${body.synced} documents`);
      return coverage.join(" · ");
    },
  );
});

const pointForm = document.querySelector("#point-sync-form");
pointForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  const locations = pointForm.querySelector("#locations").value
    .split(/\r?\n/)
    .map((location) => location.trim())
    .filter(Boolean)
    .map((location) => ({ value: location, label: location }));
  const sourceTypes = selectedValues(pointForm, "source_types");
  const limit = Number(pointForm.querySelector("#limit").value);

  if (!locations.length || !sourceTypes.length) {
    showProgress("Location synchronization", 1);
    progressStatus.textContent = "Enter at least one location and select one source type.";
    return;
  }

  runSequential(
    locations,
    "Location synchronization",
    (location) => fetch("/weather/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        locations: [location.value],
        source_types: sourceTypes,
        limit,
      }),
    }),
    (body) => `${body.synced} documents`,
  );
});

const savedSync = sessionStorage.getItem("weather-sync-results");
if (savedSync) {
  sessionStorage.removeItem("weather-sync-results");
  try {
    const summary = JSON.parse(savedSync);
    showProgress(summary.title, summary.results.length);
    progressBar.value = summary.results.length;
    progressCount.textContent = `${summary.results.length} / ${summary.results.length}`;
    progressStatus.textContent = `Complete: ${summary.totalSynced} documents synced. The Lakebase summary has been refreshed.`;
    summary.results.forEach((result) => {
      appendResult(result.label, result.status, result.detail);
    });
  } catch (_error) {
    sessionStorage.removeItem("weather-sync-results");
  }
}
