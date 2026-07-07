// Remove Python bytecode before packing so a local `npm pack`/`npm publish`
// from a dirty tree never ships __pycache__/*.pyc (CI checkouts are already clean).
const fs = require('fs');
const path = require('path');
function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name === '__pycache__') fs.rmSync(p, { recursive: true, force: true });
      else walk(p);
    } else if (p.endsWith('.pyc') || p.endsWith('.pyo')) {
      fs.rmSync(p, { force: true });
    }
  }
}
try { walk(path.join(__dirname, '..', 'backend')); } catch (e) {}
