"use strict";

require("dotenv").config();

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const qrcode = require("qrcode-terminal");

const logic = require("./logic");
const { searchTitles, createBotRequest } = require("./flaskClient");

const FLASK_BASE_URL = process.env.FLASK_BASE_URL || "http://localhost:8000";
const BOT_SHARED_SECRET = process.env.BOT_SHARED_SECRET || "";
const ALLOWED_GROUP_JIDS = (process.env.ALLOWED_GROUP_JIDS || "")
  .split(",").map((s) => s.trim()).filter(Boolean);
const DEFAULT_QUALITY = process.env.DEFAULT_QUALITY || "1080p";
const SESSION_DIR = process.env.SESSION_DIR || "./auth";
const TOP_RESULTS_LIMIT = 5;
const STATE_TIMEOUT_MS = 10 * 60 * 1000; // conversation abandonnée après 10 min d'inactivité

if (!BOT_SHARED_SECRET) {
  console.error(
    "[bot] BOT_SHARED_SECRET manquant (voir .env.example). Récupère-le sur " +
    "Configuration → Demandes (WhatsApp) dans rss-keyword-tracker."
  );
  process.exit(1);
}

// Une conversation par (groupe, utilisateur) : plusieurs membres du canal
// peuvent faire une demande en parallèle sans se marcher dessus.
const conversations = new Map();

function clearConversation(key) {
  const conv = conversations.get(key);
  if (conv && conv.timer) clearTimeout(conv.timer);
  conversations.delete(key);
}

function touchConversation(key, conv) {
  if (conv.timer) clearTimeout(conv.timer);
  conv.timer = setTimeout(() => {
    conversations.delete(key);
    console.log(`[bot] conversation ${key} expirée (inactivité)`);
  }, STATE_TIMEOUT_MS);
  conversations.set(key, conv);
}

async function reply(sock, chatJid, senderJid, text) {
  const mentionTag = `@${senderJid.split("@")[0]}`;
  await sock.sendMessage(chatJid, { text: `${mentionTag} ${text}`, mentions: [senderJid] });
}

async function handleMessage(sock, msg) {
  const chatJid = msg.key.remoteJid;

  if (!msg.message || msg.key.fromMe) {
    console.log(`[bot][debug] message ignoré (vide ou envoyé par le bot lui-même) — chat=${chatJid}`);
    return;
  }
  if (!chatJid || !chatJid.endsWith("@g.us")) {
    // Un canal WhatsApp "Channel" (diffusion, @newsletter) n'est PAS un groupe
    // (@g.us) : le bot ne peut ni le lire ni y répondre comme un membre. Il lui
    // faut un vrai groupe/communauté WhatsApp pour fonctionner.
    console.log(`[bot][debug] message hors groupe ignoré — jid=${chatJid} (types gérés : @g.us uniquement)`);
    return;
  }

  if (ALLOWED_GROUP_JIDS.length && !ALLOWED_GROUP_JIDS.includes(chatJid)) {
    // Utile lors de la première config : on log le JID pour que l'utilisateur
    // le récupère et le mette dans ALLOWED_GROUP_JIDS.
    console.log(`[bot] message ignoré, groupe non autorisé (ALLOWED_GROUP_JIDS) : ${chatJid}`);
    return;
  }

  const senderJid = msg.key.participant || msg.key.remoteJid;
  const text = logic.extractMessageText(msg.message).trim();
  console.log(`[bot][debug] message reçu — groupe=${chatJid} de=${senderJid} pushName=${msg.pushName} texte="${text}"`);
  if (!text) {
    console.log("[bot][debug] type de message non supporté (pas de texte extrait — image/audio/réaction/etc.), ignoré");
    return;
  }

  const key = logic.conversationKey(chatJid, senderJid);
  const existing = conversations.get(key);

  if (!existing) {
    // Il faut une mention explicite du bot pour démarrer une conversation ;
    // une fois lancée, les messages suivants de ce même utilisateur dans ce
    // même groupe sont traités comme la suite, sans re-mention nécessaire.
    // sock.user.id (JID classique) et sock.user.lid (identifiant "@lid" que
    // WhatsApp utilise désormais dans certains groupes) sont deux formats
    // différents pour la même identité du bot : une mention peut arriver dans
    // l'un ou l'autre selon le groupe, d'où la vérification contre les deux.
    const botJids = [sock.user.id, sock.user.lid].filter(Boolean);
    const mentioned = logic.isBotMentioned(msg.message, botJids);
    console.log(
      `[bot][debug] pas de conversation en cours — mention détectée : ${mentioned} ` +
      `(bot id=${logic.normalizeJid(sock.user.id)}, bot lid=${sock.user.lid ? logic.normalizeJid(sock.user.lid) : "indisponible"}, ` +
      `mentionedJid=${JSON.stringify(logic.extractMentionedJids(msg.message))})`
    );
    if (!mentioned) return;
    const conv = { step: "awaiting_query", data: {} };
    touchConversation(key, conv);
    await reply(sock, chatJid, senderJid, "Salut ! Quel film ou quelle série veux-tu ? Réponds avec le titre.");
    return;
  }

  await advanceConversation(sock, chatJid, senderJid, key, existing, text, msg.pushName);
}

async function advanceConversation(sock, chatJid, senderJid, key, conv, text, pushName) {
  if (logic.parseCancel(text)) {
    clearConversation(key);
    await reply(sock, chatJid, senderJid, "Demande annulée.");
    return;
  }

  if (conv.step === "awaiting_query") {
    const { results, error } = await searchTitles(FLASK_BASE_URL, text);
    if (error) {
      await reply(sock, chatJid, senderJid, `Erreur de recherche : ${error}. Réessaie avec un autre titre.`);
      return;
    }
    const top = results.slice(0, TOP_RESULTS_LIMIT);
    if (!top.length) {
      await reply(sock, chatJid, senderJid, "Aucun résultat. Réessaie avec un autre titre, ou réponds 'annuler'.");
      return;
    }
    conv.data.results = top;
    conv.step = "awaiting_confirm";
    touchConversation(key, conv);
    await reply(
      sock, chatJid, senderJid,
      `Voici ce que j'ai trouvé :\n${logic.formatResultsList(top)}\n\n` +
      "Réponds avec le numéro du résultat associé, ou 'annuler'."
    );
    return;
  }

  if (conv.step === "awaiting_confirm") {
    const n = logic.parseSelectionNumber(text, conv.data.results.length);
    if (n === null) {
      await reply(sock, chatJid, senderJid, "Réponds avec un numéro de la liste ci-dessus, ou 'annuler'.");
      return;
    }
    const selected = conv.data.results[n - 1];
    conv.data.selected = selected;
    if (selected.type === "tv") {
      conv.step = "awaiting_season";
      touchConversation(key, conv);
      await reply(sock, chatJid, senderJid, `« ${selected.title} » confirmé. Quelle saison veux-tu ? (numéro, ou 'toutes')`);
    } else {
      await finalizeRequest(sock, chatJid, senderJid, key, conv, pushName);
    }
    return;
  }

  if (conv.step === "awaiting_season") {
    const parsed = logic.parseSeasonReply(text);
    if (parsed === null) {
      await reply(sock, chatJid, senderJid, "Réponds avec un numéro de saison, ou 'toutes'.");
      return;
    }
    conv.data.season = parsed.season;
    conv.step = "awaiting_episodes";
    touchConversation(key, conv);
    await reply(sock, chatJid, senderJid, "Quels épisodes ? (ex: 1,3,5-8, ou 'tous')");
    return;
  }

  if (conv.step === "awaiting_episodes") {
    const parsed = logic.parseEpisodesReply(text);
    if (parsed === null) {
      await reply(sock, chatJid, senderJid, "Format non reconnu. Exemple : 1,3,5-8 — ou réponds 'tous'.");
      return;
    }
    conv.data.episodes = parsed.episodes;
    await finalizeRequest(sock, chatJid, senderJid, key, conv, pushName);
  }
}

async function finalizeRequest(sock, chatJid, senderJid, key, conv, pushName) {
  const selected = conv.data.selected;
  const requestedBy = pushName || senderJid.split("@")[0];
  const payload = {
    tmdb_id: selected.tmdb_id,
    type: selected.type,
    title: selected.title,
    year: selected.year || "",
    poster: selected.poster_url || null,
    quality: DEFAULT_QUALITY,
    auto_season: (conv.data.season || ""),
    auto_episodes: (conv.data.episodes || ""),
    requested_by: requestedBy,
  };
  const result = await createBotRequest(FLASK_BASE_URL, BOT_SHARED_SECRET, payload);
  clearConversation(key);

  if (!result.ok) {
    await reply(
      sock, chatJid, senderJid,
      `Erreur lors de la création de la demande : ${result.error}. Réessaie plus tard, ou préviens un admin du groupe.`
    );
    return;
  }

  const seasonBit = selected.type === "tv"
    ? ` (saison ${conv.data.season || "toutes"}, épisodes ${conv.data.episodes || "tous"})`
    : "";
  await reply(
    sock, chatJid, senderJid,
    `C'est parti ! « ${selected.title} »${seasonBit} sera téléchargé automatiquement dès qu'il est disponible.`
  );
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);

  // La version de protocole "WhatsApp Web" figée dans la lib devient obsolète
  // au fil du temps (WhatsApp la fait évoluer côté serveur) : sans version à
  // jour, le serveur rejette la poignée de main WebSocket avant même d'arriver
  // à l'échange d'authentification (symptôme typique : "connexion fermée (code
  // 405)" en boucle, jamais de QR code affiché). On la récupère dynamiquement
  // à chaque démarrage plutôt que de dépendre du défaut embarqué dans la lib.
  let version;
  try {
    const latest = await fetchLatestBaileysVersion();
    version = latest.version;
    console.log(`[bot] version WhatsApp Web : ${version.join(".")} (${latest.isLatest ? "à jour" : "pas la dernière, mais utilisée quand même"})`);
  } catch (err) {
    console.warn("[bot] impossible de récupérer la dernière version WhatsApp Web, on utilise celle par défaut de la lib :", err.message);
  }

  const sock = makeWASocket({
    auth: state,
    ...(version ? { version } : {}),
    logger: pino({ level: "silent" }),
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      console.log("[bot] Scanne ce QR code avec WhatsApp (Appareils liés) :");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "close") {
      const statusCode = lastDisconnect && lastDisconnect.error && lastDisconnect.error.output
        ? lastDisconnect.error.output.statusCode
        : null;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      console.log(
        `[bot] connexion fermée (code ${statusCode}) — ` +
        (loggedOut ? "déconnecté, il faut rescanner le QR code." : "reconnexion...")
      );
      if (!loggedOut) start().catch((err) => console.error("[bot] échec de la reconnexion :", err));
    } else if (connection === "open") {
      console.log("[bot] connecté à WhatsApp ✓");
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    console.log(`[bot][debug] messages.upsert reçu — type=${type}, count=${messages.length}`);
    if (type !== "notify") return;
    for (const msg of messages) {
      try {
        await handleMessage(sock, msg);
      } catch (err) {
        console.error("[bot] erreur en traitant un message :", err);
      }
    }
  });
}

// require.main check : permet de require() ce module depuis les tests (pour
// exercer handleMessage/advanceConversation avec un sock simulé) sans
// déclencher une vraie connexion WhatsApp. En exécution normale (node
// index.js / npm start), start() se lance comme avant.
if (require.main === module) {
  start().catch((err) => {
    console.error("[bot] erreur fatale au démarrage :", err);
    process.exit(1);
  });
}

module.exports = { handleMessage, advanceConversation, finalizeRequest };
