"use strict";

// Petit client HTTP vers l'app Flask rss-keyword-tracker. `fetchImpl` est
// injectable (par défaut le fetch global de Node) pour pouvoir tester ce
// module sans réseau réel.

const TIMEOUT_MS = 10000;

function trimSlash(url) {
  return url.replace(/\/+$/, "");
}

async function searchTitles(baseUrl, query, fetchImpl = fetch) {
  const url = `${trimSlash(baseUrl)}/tmdb/search?q=${encodeURIComponent(query)}`;
  let res;
  try {
    res = await fetchImpl(url, { signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch (err) {
    return { results: [], error: `erreur réseau (${err.message})` };
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) return { results: [], error: data.error || `erreur HTTP ${res.status} sur ${url}` };
  return { results: data.results || [], error: null };
}

async function createBotRequest(baseUrl, secret, payload, fetchImpl = fetch) {
  const url = `${trimSlash(baseUrl)}/api/bot/requests`;
  let res;
  try {
    res = await fetchImpl(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bot-Secret": secret },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (err) {
    return { ok: false, error: `erreur réseau (${err.message})` };
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) return { ok: false, error: data.error || `erreur HTTP ${res.status} sur ${url}` };
  return { ok: true, entry: data.entry };
}

module.exports = { searchTitles, createBotRequest };
