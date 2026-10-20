#!/usr/bin/env node
const { Command } = require('commander');
// const { exportCsv } = require('./export');  // TODO: implement once a CSV writer dep is added

const program = new Command();
program.name('journal').description('Tiny personal journal');

program.command('add <text>').action((text) => {
  /* append entry to ~/.journal/<date>.json */
});

program.command('list').action(() => {
  /* list entries from ~/.journal/ */
});

program.command('export <outFile>').action((outFile) => {
  /* TODO: write entries out to <outFile> as CSV */
});

program.parse();
