/* Upload with per-file progress.
 *
 * Two things this handles that a plain <form> does not:
 *
 *  - A folder off an SD card can be 800 files. Sending 800 multipart parts in one
 *    request is slow and one timeout loses everything, so files go up in batches.
 *  - The server answers with server-sent events, one per file, which is what makes
 *    the progress bar move for real rather than sitting at 0% for 30 seconds.
 *
 * Batches are sent one after another, never in parallel: the import writes to the
 * database per file, and concurrent batches would just contend for it.
 */

const BATCH_SIZE = Math.min(50, window.PAP_MAX_FILES || 50);

const counts = { imported: 0, duplicate: 0, failed: 0 };
let totalFiles = 0;
let processed = 0;

const el = (id) => document.getElementById(id);

const STATUS_LABEL = {
  imported: "นำเข้าแล้ว",
  duplicate: "ซ้ำ — ข้าม",
  failed: "ไม่สำเร็จ",
};

function acceptable(file) {
  const name = file.name.toLowerCase();
  return name.endsWith(".bys") || name.endsWith(".zip");
}

function resetProgress() {
  counts.imported = counts.duplicate = counts.failed = 0;
  processed = 0;
  el("log").innerHTML = "";
  el("finished").hidden = true;
  el("progress-card").hidden = false;
}

function renderCounts() {
  el("c-imported").textContent = counts.imported;
  el("c-duplicate").textContent = counts.duplicate;
  el("c-failed").textContent = counts.failed;
}

function renderProgress() {
  // Zip archives expand into more files than were selected, so the bar is capped
  // rather than allowed to run past 100%.
  const pct = totalFiles ? Math.min(100, (processed / totalFiles) * 100) : 0;
  el("bar").style.width = `${pct}%`;
  el("progress-text").textContent = `${processed} / ${totalFiles} ไฟล์`;
}

function appendRow(payload) {
  const row = document.createElement("tr");
  row.className = payload.status;
  const note = payload.reason || (payload.warnings || []).join("; ") || "";
  row.innerHTML = `
    <td class="mono">${escapeHtml(payload.filename)}</td>
    <td>${STATUS_LABEL[payload.status] || payload.status}</td>
    <td>${payload.night_date || ""}</td>
    <td>${payload.minutes || ""}</td>
    <td>${payload.status === "failed" ? "" : payload.ahi}</td>
    <td>${escapeHtml(note)}</td>`;
  el("log").prepend(row);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

/* The browser's EventSource cannot POST, so the SSE stream is read off fetch()
 * directly. Events are newline-delimited and can be split across chunks, hence the
 * buffer. */
async function readEventStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);

      let name = "message";
      const dataLines = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) onEvent(name, JSON.parse(dataLines.join("\n")));
    }
  }
}

async function sendBatch(batch) {
  const form = new FormData();
  for (const file of batch) form.append("files", file, file.name);

  const response = await fetch("/upload", { method: "POST", body: form });

  if (!response.ok || !response.body) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) {
      /* not json */
    }
    throw new Error(detail);
  }

  await readEventStream(response, (name, payload) => {
    if (name === "file") {
      processed += 1;
      counts[payload.status] = (counts[payload.status] || 0) + 1;
      appendRow(payload);
      renderCounts();
      renderProgress();
    } else if (name === "error") {
      throw new Error(payload.detail);
    }
  });
}

async function upload(fileList) {
  const files = Array.from(fileList).filter(acceptable);
  if (!files.length) {
    alert("ไม่พบไฟล์ .bys หรือ .zip ในสิ่งที่เลือก");
    return;
  }

  resetProgress();
  totalFiles = files.length;
  renderCounts();
  renderProgress();

  try {
    for (let i = 0; i < files.length; i += BATCH_SIZE) {
      await sendBatch(files.slice(i, i + BATCH_SIZE));
    }
    el("progress-text").textContent = `เสร็จสิ้น — ประมวลผล ${processed} ไฟล์`;
    el("finished").hidden = false;
  } catch (err) {
    el("progress-text").textContent =
      `หยุดกลางคัน: ${err.message} — อัปโหลดใหม่ทั้งชุดได้เลย ` +
      `ไฟล์ที่นำเข้าไปแล้วจะถูกข้ามอัตโนมัติ`;
    el("progress-text").classList.add("error");
  }
}

/* wiring */
el("pick-files").addEventListener("change", (e) => upload(e.target.files));
el("pick-folder").addEventListener("change", (e) => upload(e.target.files));

const dropzone = el("dropzone");
for (const type of ["dragenter", "dragover"]) {
  dropzone.addEventListener(type, (e) => {
    e.preventDefault();
    dropzone.classList.add("over");
  });
}
for (const type of ["dragleave", "drop"]) {
  dropzone.addEventListener(type, (e) => {
    e.preventDefault();
    dropzone.classList.remove("over");
  });
}
dropzone.addEventListener("drop", (e) => upload(e.dataTransfer.files));
