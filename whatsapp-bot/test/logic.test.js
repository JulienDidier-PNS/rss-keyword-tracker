"use strict";

const assert = require("node:assert/strict");
const logic = require("../logic");

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

// --- normalizeJid ---
test("normalizeJid strips device suffix", () => {
  assert.equal(logic.normalizeJid("33612345678:12@s.whatsapp.net"), "33612345678@s.whatsapp.net");
});
test("normalizeJid leaves plain JIDs untouched", () => {
  assert.equal(logic.normalizeJid("33612345678@s.whatsapp.net"), "33612345678@s.whatsapp.net");
});
test("normalizeJid handles group JIDs", () => {
  assert.equal(logic.normalizeJid("120363012345678901@g.us"), "120363012345678901@g.us");
});
test("normalizeJid handles empty input", () => {
  assert.equal(logic.normalizeJid(""), "");
  assert.equal(logic.normalizeJid(null), "");
});

// --- extractMessageText ---
test("extractMessageText reads conversation field", () => {
  assert.equal(logic.extractMessageText({ conversation: "Silo" }), "Silo");
});
test("extractMessageText reads extendedTextMessage.text", () => {
  assert.equal(logic.extractMessageText({ extendedTextMessage: { text: "@bot salut" } }), "@bot salut");
});
test("extractMessageText returns empty for unsupported message types", () => {
  assert.equal(logic.extractMessageText({ imageMessage: { caption: "x" } }), "");
  assert.equal(logic.extractMessageText(null), "");
});

// --- isBotMentioned ---
test("isBotMentioned true when mentionedJid contains the bot (device suffix ignored)", () => {
  const message = {
    extendedTextMessage: {
      text: "@bot salut",
      contextInfo: { mentionedJid: ["33698765432@s.whatsapp.net"] },
    },
  };
  assert.equal(logic.isBotMentioned(message, "33698765432:9@s.whatsapp.net"), true);
});
test("isBotMentioned false when the bot isn't mentioned", () => {
  const message = {
    extendedTextMessage: {
      text: "salut",
      contextInfo: { mentionedJid: ["33611111111@s.whatsapp.net"] },
    },
  };
  assert.equal(logic.isBotMentioned(message, "33698765432:9@s.whatsapp.net"), false);
});
test("isBotMentioned false with no contextInfo at all", () => {
  assert.equal(logic.isBotMentioned({ conversation: "salut" }, "33698765432:9@s.whatsapp.net"), false);
});
test("isBotMentioned matches a @lid mention against a list of candidate bot JIDs (PN + LID)", () => {
  // Cas réel : WhatsApp adresse certains groupes en @lid (anti-fuite de
  // numéro) ; la mention arrive alors en @lid alors que sock.user.id du bot
  // reste en @s.whatsapp.net — il faut vérifier contre les deux identités.
  const message = {
    extendedTextMessage: {
      text: "@bot salut",
      contextInfo: { mentionedJid: ["225765677334589@lid"] },
    },
  };
  const botJids = ["33783197281:9@s.whatsapp.net", "225765677334589:9@lid"];
  assert.equal(logic.isBotMentioned(message, botJids), true);
});
test("isBotMentioned false when only the non-matching PN JID is given (regression du bug @lid)", () => {
  const message = {
    extendedTextMessage: {
      text: "@bot salut",
      contextInfo: { mentionedJid: ["225765677334589@lid"] },
    },
  };
  assert.equal(logic.isBotMentioned(message, ["33783197281:9@s.whatsapp.net"]), false);
});

// --- conversationKey ---
test("conversationKey combines chat and normalized sender", () => {
  assert.equal(
    logic.conversationKey("120363@g.us", "33612345678:5@s.whatsapp.net"),
    "120363@g.us::33612345678@s.whatsapp.net"
  );
});

// --- parseCancel ---
test("parseCancel recognizes cancel words case-insensitively", () => {
  assert.equal(logic.parseCancel("annuler"), true);
  assert.equal(logic.parseCancel("STOP"), true);
  assert.equal(logic.parseCancel("  cancel  "), true);
  assert.equal(logic.parseCancel("Silo"), false);
});

// --- parseSelectionNumber ---
test("parseSelectionNumber accepts a number within range", () => {
  assert.equal(logic.parseSelectionNumber("2", 5), 2);
});
test("parseSelectionNumber rejects out-of-range and non-numeric replies", () => {
  assert.equal(logic.parseSelectionNumber("0", 5), null);
  assert.equal(logic.parseSelectionNumber("6", 5), null);
  assert.equal(logic.parseSelectionNumber("deux", 5), null);
  assert.equal(logic.parseSelectionNumber("2.5", 5), null);
});

// --- parseSeasonReply ---
test("parseSeasonReply recognizes 'toutes' as all seasons", () => {
  assert.deepEqual(logic.parseSeasonReply("toutes"), { season: "" });
  assert.deepEqual(logic.parseSeasonReply("Tout"), { season: "" });
  assert.deepEqual(logic.parseSeasonReply("all"), { season: "" });
});
test("parseSeasonReply parses a season number", () => {
  assert.deepEqual(logic.parseSeasonReply("2"), { season: "2" });
});
test("parseSeasonReply rejects garbage", () => {
  assert.equal(logic.parseSeasonReply("saison 2"), null);
  assert.equal(logic.parseSeasonReply(""), null);
});

// --- parseEpisodesReply ---
test("parseEpisodesReply recognizes 'tous' as all episodes", () => {
  assert.deepEqual(logic.parseEpisodesReply("tous"), { episodes: "" });
});
test("parseEpisodesReply parses a valid spec, stripping spaces", () => {
  assert.deepEqual(logic.parseEpisodesReply("1, 3, 5-8"), { episodes: "1,3,5-8" });
});
test("parseEpisodesReply rejects malformed specs", () => {
  assert.equal(logic.parseEpisodesReply("1,3,,5"), null);
  assert.equal(logic.parseEpisodesReply("abc"), null);
  assert.equal(logic.parseEpisodesReply("1-"), null);
});

// --- formatResultsList ---
test("formatResultsList numbers results and shows type + year", () => {
  const out = logic.formatResultsList([
    { title: "Silo", year: "2023", type: "tv" },
    { title: "Dune", year: "2021", type: "movie" },
    { title: "Sans année", year: "", type: "movie" },
  ]);
  assert.equal(
    out,
    "1. Silo (2023) — Série\n2. Dune (2021) — Film\n3. Sans année — Film"
  );
});

console.log(`\n${passed} tests passés.`);
