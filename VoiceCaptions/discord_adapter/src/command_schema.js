import { PermissionFlagsBits, SlashCommandBuilder } from "discord.js";

const CLASS_CHOICES = [
  ["Death Knight", "death-knight"],
  ["Demon Hunter", "demon-hunter"],
  ["Druid", "druid"],
  ["Evoker", "evoker"],
  ["Hunter", "hunter"],
  ["Mage", "mage"],
  ["Monk", "monk"],
  ["Paladin", "paladin"],
  ["Priest", "priest"],
  ["Rogue", "rogue"],
  ["Shaman", "shaman"],
  ["Warlock", "warlock"],
  ["Warrior", "warrior"],
];

export const captionsCommand = new SlashCommandBuilder()
  .setName("vem")
  .setDescription("Control consent-gated raid voice captions")
  .setDMPermission(false)
  .addSubcommand((command) =>
    command
      .setName("start")
      .setDescription("Open a caption session in your current voice channel"),
  )
  .addSubcommand((command) =>
    command.setName("consent").setDescription("Allow your voice in this caption session"),
  )
  .addSubcommand((command) =>
    command
      .setName("decline")
      .setDescription("Withdraw consent - works even with no session running"),
  )
  .addSubcommand((command) =>
    command
      .setName("forget")
      .setDescription("Erase your remembered consent and character mapping"),
  )
  .addSubcommand((command) =>
    command.setName("stop").setDescription("Stop this caption session for everyone"),
  )
  .addSubcommand((command) =>
    command.setName("status").setDescription("Show capture and consent status"),
  )
  .addSubcommand((command) =>
    command
      .setName("setup")
      .setDescription("Get your own private Vemal's Ears pairing link"),
  )
  .addSubcommand((command) =>
    command
      .setName("invite")
      .setDescription("Create a pairing link for someone else (guild managers)")
      .addUserOption((option) =>
        option
          .setName("member")
          .setDescription("Who the link is for")
          .setRequired(true),
      ),
  )
  .addSubcommand((command) =>
    command
      .setName("map")
      // These two descriptions are what Discord shows the person before
      // they act. The mapping outlives the session, so saying "for this
      // run" or "in-memory" would understate what is kept about them.
      .setDescription("Map your voice to a character, remembered until you unmap")
      // All optional on purpose: running /vem map without a complete
      // mapping opens the private class modal instead. Supplying them all
      // still works in one shot for anyone who prefers typing.
      .addStringOption((option) =>
        option
          .setName("character")
          .setDescription("Your character name")
          .setMinLength(2)
          .setMaxLength(24)
          .setRequired(false),
      )
      .addStringOption((option) =>
        option
          .setName("class")
          .setDescription("Your character class")
          .setRequired(false)
          .addChoices(...CLASS_CHOICES.map(([name, value]) => ({ name, value }))),
      )
      .addStringOption((option) =>
        option
          .setName("role")
          .setDescription("Your current role")
          .setRequired(false)
          .addChoices(
            { name: "Damage", value: "damage" },
            { name: "Healer", value: "healer" },
            { name: "Tank", value: "tank" },
          ),
      )
      .addStringOption((option) =>
        option
          .setName("realm")
          .setDescription("Optional realm name")
          .setMaxLength(32)
          .setRequired(false),
      ),
  )
  .addSubcommand((command) =>
    command.setName("unmap").setDescription("Erase your stored character mapping"),
  );

export const captionsCommandJson = Object.freeze(captionsCommand.toJSON());
