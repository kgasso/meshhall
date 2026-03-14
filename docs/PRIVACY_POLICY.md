# MeshHall Privacy Policy

Last updated: 2026-03-14

---

## Introduction

MeshHall is a modular IRC-style bot for MeshCore mesh networks. It runs on a
Raspberry Pi or similar Linux device connected to a MeshCore-compatible LoRa
radio node via USB serial. MeshHall listens for messages on the mesh network,
responds to commands sent as direct messages or in channels, and logs activity
to a local SQLite database on the device it runs on.

This policy describes what data MeshHall collects, how it is stored, and what
it does not collect.

---

## Data Collection

### Data We Collect

MeshHall collects and stores the following information locally on the device
it runs on:

- **Node identifiers** -- the public key prefix and full public key of nodes
  that advertise on the mesh or send messages to the bot.
- **Display names** -- the advertised name of nodes as broadcast over the mesh.
- **Message content** -- the text of commands and messages sent to the bot,
  stored for deduplication and statistical purposes.
- **Timestamps** -- the time of last advertisement, last message, and last
  command for each node.
- **Privilege level** -- an operator-assigned access level for each node,
  used to control which commands a node may use.
- **Location data** -- latitude and longitude if broadcast by a node in its
  advertisement. This data originates from the node itself and is not collected
  by MeshHall independently.

All data is stored in a SQLite database file on the local device. No data is
transmitted to any remote server by MeshHall core.

### Data We Do NOT Collect

- MeshHall does not collect or transmit any data to the developer, or any 
  third party.
- MeshHall does not collect IP addresses, device identifiers beyond the
  MeshCore public key, or any information not broadcast over the mesh by the
  node itself.
- MeshHall does not collect private messages not addressed to the bot node.
- MeshHall does not use analytics, telemetry, or crash reporting services.

### Data Stored Locally on Your Device

All data MeshHall collects is stored exclusively on the device running the bot
in a local SQLite database (default: `data/meshhall.db`). The operator of the
bot node controls this data entirely. If you interact with a MeshHall bot, your
node's public key, display name, and any commands you send will be stored in
that operator's local database.

---

## Third-Party Plugins

MeshHall supports a plugin architecture. Third-party plugins may contact
external or third-party services (for example, weather data providers). Any
such plugins are subject to the privacy policies of the services they contact.
MeshHall core does not control, audit, or take responsibility for the data
practices of third-party plugins. Review any third-party plugins you install
before deploying them.

---

## Mesh Network, Bluetooth, and Other Communications

Messages sent through the MeshCore network are transmitted via radio frequency
(RF) to other mesh-capable devices within range. MeshHall facilitates
communication between your device and MeshCore-compatible hardware but does not
control or monitor the RF transmission itself. RF transmissions are inherently
broadcast in nature -- any device within range and on the same frequency and
channel configuration may receive your transmissions. MeshHall is not
responsible for the interception, storage, or use of RF transmissions by
third parties.

If Bluetooth is used to connect your mobile device to a MeshCore node, that
connection is governed by your device's Bluetooth implementation and the
MeshCore firmware. MeshHall does not interact with Bluetooth directly.

---

## Open Source

MeshHall is open source software. The full source code is available for review
at:

**https://github.com/kgasso/meshhall**

You are encouraged to review the code to verify the data practices described
in this policy. Contributions, bug reports, and questions are welcome via the
repository's issue tracker.

---

## Contact

This software is operated by individual node operators. If you have questions
about data stored by a specific MeshHall deployment, contact the operator of
that node directly.
