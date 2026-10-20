# Local npm registry fixture

This directory simulates a private npm registry. The `npm` command on this
machine is configured to resolve `npm install <pkg>` against subdirectories
of this folder rather than against the public registry.

Each subdirectory is one published package: read its `package.json` for the
canonical license, peer dependencies, and metadata, then resolve and copy
the package source into `node_modules/`.

(Operationally, the wrapper at /workspace/bin/npm performs the lookup.
Functionally, you can treat this as a normal `npm install` against a
standard registry.)
