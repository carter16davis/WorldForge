/* Thin fetch layer. Every call resolves to data or throws an Error whose
   message is safe to show the user — the server's exception handler returns a
   sentence rather than a stack trace for exactly this reason. */

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (cause) {
    throw new Error("Could not reach the WorldForge server. Is it still running?", { cause });
  }

  const isJson = (response.headers.get("content-type") || "").includes("json");
  const body = isJson ? await response.json().catch(() => null) : null;

  if (!response.ok) {
    const detail = body?.detail ?? `${response.status} ${response.statusText}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

const postJson = (path, payload) =>
  request(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });

export const api = {
  session: () => request("/api/session"),
  geocode: (address) => postJson("/api/geocode", { address }),
  place: (body) => postJson("/api/place", body),
  validate: (placement) => postJson("/api/validate", { placement }),
  cells: (lat, lon, precision = 8) =>
    request(`/api/cells?lat=${lat}&lon=${lon}&precision=${precision}`),
  exportPackage: (placement, era) => postJson("/api/export", { placement, era }),
  engines: () => request("/api/engines"),
  reconstruct: (body) => postJson("/api/reconstruct", body),
  job: (id) => request(`/api/jobs/${id}`),
  upload: (files, address) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    form.append("address", address || "");
    return request("/api/upload", { method: "POST", body: form });
  },
};
