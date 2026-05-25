---
name: user-fire-tablet
description: "User owns a Fire HD 10 Plus 11th gen running Fire OS 7.3.2.7, plugged in via USB for development"
metadata: 
  node_type: memory
  type: user
  originSessionId: 56052ee6-6721-45b2-ac0d-5d39b2a23e80
---

User owns an Amazon Fire HD 10 Plus, 11th generation, running Fire OS 7.3.2.7 (Android 9 base). Device codename "trona", model `KFTRPWI`, ADB serial `G001MG06142201BG`. Connected to this PC over USB. ADB is enabled with USB mode "File Transfer" and this PC is in the always-allowed list.

Tablet-side gotcha: USB Preferences "Use USB for" radios are greyed out until "USB controlled by" is set to "This device". File Transfer must be selected for ADB to expose its endpoint.

User chose to stay on Fire OS 7 rather than update to 8 — discussed and agreed updates are irreversible and offer no benefit for our `minSdk 22` target.
