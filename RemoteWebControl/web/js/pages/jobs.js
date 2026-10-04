// Job list. Refreshes itself while any job is queued or slicing.

import { api, errorMessage, messageFor } from "../api.js";
import { el, formatDate, jobTitle, stateBadge } from "../util.js";
import { t } from "../i18n.js";

export async function renderJobs(view, _params, context) {
  context.setTitle(t("app.name"));
  const list = el("div", { class: "stack" });
  const newButton = el("a", { class: "button primary fab", href: "#/new", text: t("jobs.new") });
  view.append(list, newButton);

  let timer = null;
  let stopped = false;

  async function refresh() {
    let jobs;
    try {
      jobs = await api.jobs();
    } catch (e) {
      list.replaceChildren(el("div", { class: "card muted", text: errorMessage(e) }),
        el("button", { class: "block", text: t("jobs.retry"), onclick: refresh }));
      return;
    }
    if (stopped) return;
    if (!jobs.length) {
      list.replaceChildren(el("div", { class: "card center stack" },
        el("h2", { text: t("jobs.empty") }),
        el("p", { class: "muted", text: t("jobs.empty_hint") }),
        el("a", { class: "button primary", href: "#/new", text: t("jobs.upload") })));
    } else {
      list.replaceChildren(...jobs.map(jobItem));
    }
    clearTimeout(timer);
    if (jobs.some((job) => job.state === "queued" || job.state === "slicing")) {
      timer = setTimeout(refresh, 2000);
    }
  }

  await refresh();
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}

function jobItem(job) {
  const busy = job.state === "queued" || job.state === "slicing";
  return el("a", { class: "card job-item", href: `#/job/${job.id}` },
    el("div", { class: "row" },
      el("div", { class: "grow name", text: jobTitle(job) }),
      stateBadge(job.state)),
    el("div", { class: "muted small", text: `${job.printer_id} · ${formatDate(job.created_at)}` }),
    busy ? el("div", { class: "progress", style: "margin-top: 8px" },
      el("div", { style: `width: ${Math.round((job.progress || 0) * 100)}%` })) : null,
    job.state === "error" && job.error ? el("div", { class: "small", style: "color: var(--error); margin-top: 6px",
      text: messageFor(job.error) }) : null);
}

