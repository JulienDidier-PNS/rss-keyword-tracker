"use strict";

// Test d'intégration de la machine à états (mention -> recherche -> choix ->
// saison/épisodes -> création de la demande), sans connexion WhatsApp réelle
// ni appel réseau vers Flask : on simule `sock` et on intercepte `fetch`.

process.env.BOT_SHARED_SECRET = "test-secret";
process.env.FLASK_BASE_URL = "http://flask.test";
process.env.DEFAULT_QUALITY = "1080p";

const assert = require("node:assert/strict");

const BOT_JID = "10000000000:5@s.whatsapp.net";
const BOT_LID = "20000000000:5@lid";
const GROUP_JID = "120363012345678901@g.us";
const USER_JID = "33612345678@s.whatsapp.net";

const TV_RESULTS = [
  { tmdb_id: 94997, type: "tv", title: "House of the Dragon", year: "2022", poster_url: "http://x/p.jpg" },
  { tmdb_id: 12345, type: "tv", title: "House of Cards", year: "2013", poster_url: null },
];
const MOVIE_RESULTS = [
  { tmdb_id: 438631, type: "movie", title: "Dune", year: "2021", poster_url: "http://x/dune.jpg" },
];
// Titres dédiés pour simuler les statuts already_available / needs_review
// renvoyés par Flask (voir check_plex_availability côté Python).
const ALREADY_AVAILABLE_RESULTS = [
  { tmdb_id: 999001, type: "movie", title: "Already Here", year: "2020", poster_url: null },
];
const NEEDS_REVIEW_RESULTS = [
  { tmdb_id: 999002, type: "movie", title: "Ambiguous Movie", year: "2019", poster_url: null },
];

let lastBotRequestPayload = null;

global.fetch = async (url, opts) => {
  if (url.includes("/tmdb/search")) {
    const query = decodeURIComponent(url.split("q=")[1] || "").toLowerCase();
    let results = TV_RESULTS;
    if (query.includes("dune")) results = MOVIE_RESULTS;
    else if (query.includes("already here")) results = ALREADY_AVAILABLE_RESULTS;
    else if (query.includes("ambiguous")) results = NEEDS_REVIEW_RESULTS;
    return { ok: true, status: 200, json: async () => ({ ok: true, results }) };
  }
  if (url.includes("/api/bot/requests")) {
    lastBotRequestPayload = JSON.parse(opts.body);
    const p = lastBotRequestPayload;

    if (p.title === "Already Here") {
      return {
        ok: true, status: 200,
        json: async () => ({
          ok: true, status: "already_available", entry: null,
          message: `« ${p.title} » est déjà disponible sur Plex — rien à télécharger.`,
        }),
      };
    }
    if (p.title === "Ambiguous Movie") {
      return {
        ok: true, status: 200,
        json: async () => ({
          ok: true, status: "needs_review", entry: { ...p, origin: "whatsapp", review_status: "needs_review" },
          message: "Vérification Plex/Tautulli impossible (erreur réseau) : ajouté quand même, à vérifier manuellement.",
        }),
      };
    }

    // Reproduit le message que Flask calcule réellement (create_bot_request),
    // pour vérifier que le bot le relaie tel quel sans le reformuler.
    const seasonBit = p.type === "tv" ? ` (saison ${p.auto_season}, épisodes ${p.auto_episodes || "tous"})` : "";
    const message = `« ${p.title} »${seasonBit} ajouté en téléchargement automatique (${p.quality}).`;
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true, status: "created", entry: { ...p, origin: "whatsapp" }, message }),
    };
  }
  throw new Error(`URL inattendue dans le test : ${url}`);
};

const bot = require("../index.js"); // require.main !== module ici : start() ne se lance pas

function makeSock({ withLid = false } = {}) {
  return {
    user: withLid ? { id: BOT_JID, lid: BOT_LID } : { id: BOT_JID },
    sent: [],
    async sendMessage(jid, content) {
      this.sent.push({ jid, ...content });
    },
  };
}

function textMessage(text, { mentioned = false, mentionLid = false, senderJid = USER_JID } = {}) {
  const mentionTarget = mentionLid ? BOT_LID : BOT_JID;
  const contextInfo = mentioned ? { mentionedJid: [mentionTarget] } : undefined;
  return {
    key: { remoteJid: GROUP_JID, participant: senderJid, fromMe: false },
    pushName: "Alice",
    message: { extendedTextMessage: { text, contextInfo } },
  };
}

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

async function main() {
  await test("un message sans mention n'ouvre pas de conversation", async () => {
    const sock = makeSock();
    await bot.handleMessage(sock, textMessage("Silo", { mentioned: false }));
    assert.equal(sock.sent.length, 0);
  });

  await test("une mention en @lid est reconnue quand sock.user.lid est disponible (bug réel corrigé)", async () => {
    const sock = makeSock({ withLid: true });
    const sender = "33600000001@s.whatsapp.net"; // expéditeur dédié : évite toute conversation résiduelle d'un autre test
    await bot.handleMessage(sock, textMessage("@bot", { mentioned: true, mentionLid: true, senderJid: sender }));
    assert.equal(sock.sent.length, 1, "la conversation doit démarrer même si la mention est en @lid");
    assert.match(sock.sent[0].text, /quel film ou quelle série/i);
  });

  await test("une mention en @lid n'est PAS reconnue si sock.user.lid est indisponible (régression documentée)", async () => {
    const sock = makeSock({ withLid: false });
    const sender = "33600000002@s.whatsapp.net"; // expéditeur dédié : évite toute conversation résiduelle d'un autre test
    await bot.handleMessage(sock, textMessage("@bot", { mentioned: true, mentionLid: true, senderJid: sender }));
    assert.equal(sock.sent.length, 0, "sans sock.user.lid, une mention @lid ne peut pas être rapprochée de l'identité PN du bot");
  });

  await test("parcours complet série : mention -> recherche -> choix -> saison -> épisodes -> création", async () => {
    lastBotRequestPayload = null;
    const sock = makeSock();

    await bot.handleMessage(sock, textMessage("@bot", { mentioned: true }));
    assert.equal(sock.sent.length, 1);
    assert.match(sock.sent[0].text, /quel film ou quelle série/i);

    await bot.handleMessage(sock, textMessage("house of the dragon"));
    assert.equal(sock.sent.length, 2);
    assert.match(sock.sent[1].text, /1\. House of the Dragon \(2022\) — Série/);
    assert.match(sock.sent[1].text, /2\. House of Cards \(2013\) — Série/);

    await bot.handleMessage(sock, textMessage("1"));
    assert.equal(sock.sent.length, 3);
    assert.match(sock.sent[2].text, /House of the Dragon.*confirmé.*saison/i);

    await bot.handleMessage(sock, textMessage("2"));
    assert.equal(sock.sent.length, 4);
    assert.match(sock.sent[3].text, /quels épisodes/i);

    await bot.handleMessage(sock, textMessage("1,3,5-8"));
    assert.equal(sock.sent.length, 5);
    assert.match(sock.sent[4].text, /ajouté en téléchargement automatique/);
    assert.match(sock.sent[4].text, /1080p/);

    assert.deepEqual(lastBotRequestPayload, {
      tmdb_id: 94997,
      type: "tv",
      title: "House of the Dragon",
      year: "2022",
      poster: "http://x/p.jpg",
      quality: "1080p",
      auto_season: "2",
      auto_episodes: "1,3,5-8",
      requested_by: "Alice",
    });

    // La conversation doit être terminée : un nouveau message sans mention est ignoré.
    await bot.handleMessage(sock, textMessage("encore un truc"));
    assert.equal(sock.sent.length, 5);
  });

  await test("un film ne demande pas de saison/épisodes et part directement", async () => {
    lastBotRequestPayload = null;
    const sock = makeSock();
    await bot.handleMessage(sock, textMessage("@bot", { mentioned: true }));
    await bot.handleMessage(sock, textMessage("dune"));
    assert.match(sock.sent[sock.sent.length - 1].text, /1\. Dune \(2021\) — Film/);

    await bot.handleMessage(sock, textMessage("1"));
    assert.equal(sock.sent.length, 3);
    assert.match(sock.sent[2].text, /ajouté en téléchargement automatique/);
    assert.doesNotMatch(sock.sent[2].text, /saison/i);

    assert.deepEqual(lastBotRequestPayload, {
      tmdb_id: 438631,
      type: "movie",
      title: "Dune",
      year: "2021",
      poster: "http://x/dune.jpg",
      quality: "1080p",
      auto_season: "",
      auto_episodes: "",
      requested_by: "Alice",
    });
  });

  await test("titre déjà disponible sur Plex : le message de Flask est relayé tel quel", async () => {
    const sock = makeSock();
    const sender = "33600000010@s.whatsapp.net";
    await bot.handleMessage(sock, textMessage("@bot", { mentioned: true, senderJid: sender }));
    await bot.handleMessage(sock, textMessage("already here", { senderJid: sender }));
    await bot.handleMessage(sock, textMessage("1", { senderJid: sender }));
    assert.equal(sock.sent.length, 3);
    assert.match(sock.sent[2].text, /déjà disponible sur Plex/);
  });

  await test("vérification Plex incertaine : message 'à vérifier' relayé tel quel", async () => {
    const sock = makeSock();
    const sender = "33600000011@s.whatsapp.net";
    await bot.handleMessage(sock, textMessage("@bot", { mentioned: true, senderJid: sender }));
    await bot.handleMessage(sock, textMessage("ambiguous", { senderJid: sender }));
    await bot.handleMessage(sock, textMessage("1", { senderJid: sender }));
    assert.equal(sock.sent.length, 3);
    assert.match(sock.sent[2].text, /à vérifier manuellement/);
  });

  await test("annuler nettoie la conversation à n'importe quelle étape", async () => {
    const sock = makeSock();
    await bot.handleMessage(sock, textMessage("@bot", { mentioned: true }));
    await bot.handleMessage(sock, textMessage("annuler"));
    assert.match(sock.sent[sock.sent.length - 1].text, /annulée/);
    // Après annulation, il faut re-mentionner pour relancer une conversation.
    await bot.handleMessage(sock, textMessage("silo"));
    assert.equal(sock.sent.length, 2); // pas de 3e message : ignoré faute de mention
  });

  await test("un choix hors liste redemande poliment sans planter", async () => {
    const sock = makeSock();
    await bot.handleMessage(sock, textMessage("@bot", { mentioned: true }));
    await bot.handleMessage(sock, textMessage("house of the dragon"));
    await bot.handleMessage(sock, textMessage("99"));
    assert.match(sock.sent[sock.sent.length - 1].text, /numéro de la liste/);
  });

  await test("ALLOWED_GROUP_JIDS ignore les messages venant d'un groupe non listé", async () => {
    // ALLOWED_GROUP_JIDS est lu une fois au chargement du module : on force un
    // rechargement à chaud avec un autre groupe autorisé pour vérifier le filtre.
    process.env.ALLOWED_GROUP_JIDS = "999999999999999999@g.us";
    delete require.cache[require.resolve("../index.js")];
    const restrictedBot = require("../index.js");

    const sock = makeSock();
    await restrictedBot.handleMessage(sock, textMessage("@bot", { mentioned: true }));
    assert.equal(sock.sent.length, 0, "le groupe de test n'est pas dans ALLOWED_GROUP_JIDS : rien ne doit partir");

    delete require.cache[require.resolve("../index.js")];
    process.env.ALLOWED_GROUP_JIDS = "";
  });

  console.log(`\n${passed} tests passés.`);
}

main()
  .then(() => process.exit(0)) // certains tests laissent une conversation ouverte avec un
  // minuteur de 10 min (touchConversation) : sans exit explicite, le process
  // Node reste vivant jusqu'à son expiration naturelle avant de rendre la main.
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
