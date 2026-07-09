"use strict";

const assert = require("node:assert/strict");
const { searchTitles, createBotRequest } = require("../flaskClient");

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

function fakeFetch(status, jsonBody) {
  return async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => jsonBody,
  });
}

function throwingFetch(message) {
  return async () => {
    throw new Error(message);
  };
}

async function main() {
  await test("searchTitles returns results on success", async () => {
    const fetchImpl = fakeFetch(200, { ok: true, results: [{ title: "Silo" }] });
    const { results, error } = await searchTitles("http://x", "silo", fetchImpl);
    assert.equal(error, null);
    assert.deepEqual(results, [{ title: "Silo" }]);
  });

  await test("searchTitles surfaces the Flask-provided error", async () => {
    const fetchImpl = fakeFetch(200, { ok: false, error: "clé TMDB manquante" });
    const { results, error } = await searchTitles("http://x", "silo", fetchImpl);
    assert.deepEqual(results, []);
    assert.equal(error, "clé TMDB manquante");
  });

  await test("searchTitles handles network errors gracefully", async () => {
    const { results, error } = await searchTitles("http://x", "silo", throwingFetch("ECONNREFUSED"));
    assert.deepEqual(results, []);
    assert.match(error, /ECONNREFUSED/);
  });

  await test("searchTitles reports a bare HTTP error (e.g. 404) with the exact URL requested", async () => {
    // Cas réel : mauvais FLASK_BASE_URL ou route absente -> Flask/le serveur en
    // face répond 404 sans JSON exploitable. Le message doit rester exploitable
    // pour du debug (montrer l'URL exacte tapée), pas juste "erreur HTTP 404".
    const fetchImpl = fakeFetch(404, {});
    const { results, error } = await searchTitles("http://flask.test", "house of the dragon", fetchImpl);
    assert.deepEqual(results, []);
    assert.equal(error, "erreur HTTP 404 sur http://flask.test/tmdb/search?q=house%20of%20the%20dragon");
  });

  await test("createBotRequest returns the created entry on success", async () => {
    const fetchImpl = fakeFetch(200, { ok: true, entry: { title: "Silo", quality: "1080p" } });
    const result = await createBotRequest("http://x", "secret", { title: "Silo" }, fetchImpl);
    assert.equal(result.ok, true);
    assert.deepEqual(result.entry, { title: "Silo", quality: "1080p" });
  });

  await test("createBotRequest surfaces a 401 as a clear error", async () => {
    const fetchImpl = fakeFetch(401, { ok: false, error: "secret invalide" });
    const result = await createBotRequest("http://x", "wrong-secret", { title: "Silo" }, fetchImpl);
    assert.equal(result.ok, false);
    assert.equal(result.error, "secret invalide");
  });

  await test("createBotRequest handles network errors gracefully", async () => {
    const result = await createBotRequest("http://x", "secret", {}, throwingFetch("timeout"));
    assert.equal(result.ok, false);
    assert.match(result.error, /timeout/);
  });

  console.log(`\n${passed} tests passés.`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
