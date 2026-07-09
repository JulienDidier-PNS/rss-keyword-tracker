"use strict";

// Fonctions pures (aucun appel réseau/Baileys) : la machine à états et le
// parsing des réponses utilisateur vivent ici pour rester testables sans
// connexion WhatsApp réelle. index.js ne fait que les brancher sur Baileys.

const ALL_WORDS_RE = /^(tout|toutes?|tous|all)$/i;
const CANCEL_WORDS_RE = /^(annuler|stop|cancel|non merci)$/i;
const EPISODES_SPEC_RE = /^\d+(-\d+)?(,\d+(-\d+)?)*$/;

// Un JID Baileys peut porter un suffixe d'appareil ("<user>:<device>@host"),
// ex. l'identité du bot (sock.user.id) vs les mentionedJid (sans suffixe) : on
// normalise les deux formes pour pouvoir les comparer.
function normalizeJid(jid) {
  if (!jid) return "";
  const at = jid.indexOf("@");
  if (at === -1) return jid;
  const user = jid.slice(0, at).split(":")[0];
  const host = jid.slice(at + 1);
  return `${user}@${host}`;
}

function extractMessageText(message) {
  if (!message) return "";
  if (typeof message.conversation === "string") return message.conversation;
  if (message.extendedTextMessage && typeof message.extendedTextMessage.text === "string") {
    return message.extendedTextMessage.text;
  }
  return "";
}

function extractMentionedJids(message) {
  const ctx = message && message.extendedTextMessage && message.extendedTextMessage.contextInfo;
  return (ctx && ctx.mentionedJid) || [];
}

// botJids : un JID ou une liste de JIDs candidats pour "le bot". Nécessaire
// car WhatsApp adresse désormais certains groupes en "@lid" (identifiant
// anti-fuite de numéro) plutôt qu'en "@s.whatsapp.net" classique : une mention
// peut donc arriver dans un format différent de sock.user.id selon le groupe,
// d'où la vérification contre plusieurs identités possibles du bot.
function isBotMentioned(message, botJids) {
  const targets = (Array.isArray(botJids) ? botJids : [botJids]).map(normalizeJid).filter(Boolean);
  if (!targets.length) return false;
  const mentioned = extractMentionedJids(message).map(normalizeJid);
  return mentioned.some((jid) => targets.includes(jid));
}

function conversationKey(chatJid, senderJid) {
  return `${chatJid}::${normalizeJid(senderJid)}`;
}

function parseCancel(text) {
  return CANCEL_WORDS_RE.test((text || "").trim());
}

// Numéro 1-based valide dans [1, max], ou null si la réponse n'en est pas un.
function parseSelectionNumber(text, max) {
  const t = (text || "").trim();
  if (!/^\d+$/.test(t)) return null;
  const n = parseInt(t, 10);
  if (n < 1 || n > max) return null;
  return n;
}

// {season: ""} pour "toutes les saisons", {season: "N"} pour une saison précise,
// ou null si la réponse n'est ni un mot-clé "toutes" ni un numéro plausible.
function parseSeasonReply(text) {
  const t = (text || "").trim();
  if (ALL_WORDS_RE.test(t)) return { season: "" };
  if (!/^\d+$/.test(t)) return null;
  const n = parseInt(t, 10);
  if (n < 1 || n > 999) return null;
  return { season: String(n) };
}

// {episodes: ""} pour "tous les épisodes", {episodes: "1,3,5-8"} pour une spec
// (même format que le champ auto_episodes existant), ou null si invalide.
function parseEpisodesReply(text) {
  const t = (text || "").trim();
  if (ALL_WORDS_RE.test(t)) return { episodes: "" };
  const cleaned = t.replace(/\s+/g, "");
  if (!EPISODES_SPEC_RE.test(cleaned)) return null;
  return { episodes: cleaned };
}

function formatResultsList(results) {
  return results
    .map((r, i) => {
      const kind = r.type === "tv" ? "Série" : "Film";
      const year = r.year ? ` (${r.year})` : "";
      return `${i + 1}. ${r.title}${year} — ${kind}`;
    })
    .join("\n");
}

module.exports = {
  normalizeJid,
  extractMessageText,
  extractMentionedJids,
  isBotMentioned,
  conversationKey,
  parseCancel,
  parseSelectionNumber,
  parseSeasonReply,
  parseEpisodesReply,
  formatResultsList,
};
