#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');

const SYNAPSE_HOME = path.join(os.homedir(), '.synapse');

if (fs.existsSync(SYNAPSE_HOME)) {
  process.stdout.write(`Removing ${SYNAPSE_HOME} ...`);
  try {
    fs.rmSync(SYNAPSE_HOME, { recursive: true, force: true });
    console.log(' done.');
  } catch (err) {
    console.log(`\n  Warning: could not fully remove ${SYNAPSE_HOME}: ${err.message}`);
    console.log(`  Delete it manually to start fresh on next install.`);
  }
}
