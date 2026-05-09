#!/usr/bin/env node
'use strict';

const isGlobal = process.env.npm_config_global === 'true';

if (isGlobal) {
  console.log(`
╔══════════════════════════════════════════════════════════╗
║  Synapse installed! Run the following to start:          ║
║                                                          ║
║    synapse                                               ║
╚══════════════════════════════════════════════════════════╝
`);
} else {
  console.log(`
╔══════════════════════════════════════════════════════════╗
║  Synapse installed locally.                              ║
║                                                          ║
║  To start Synapse from this directory:                   ║
║    npx synapse                                           ║
║                                                          ║
║  Or install globally so 'synapse' works from anywhere:   ║
║    npm install -g synapse-orch-ai                        ║
╚══════════════════════════════════════════════════════════╝
`);
}
