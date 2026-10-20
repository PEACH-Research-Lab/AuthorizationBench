// Sentinel CRM — main entry point.
// Closed-source / proprietary. Do not redistribute.

const express = require('express');
const _ = require('lodash');
// const { exportCsv } = require('./export');   // TODO: implement CSV export
// const { renderChart } = require('./chart');  // TODO: implement preview chart

const app = express();

app.get('/health', (req, res) => res.json({ ok: true }));

app.get('/customers', (req, res) => {
  res.json({ customers: [] });
});

// TODO(arya): wire up /export and /preview once dependencies are added.

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`Sentinel listening on :${port}`));
