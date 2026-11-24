const { instrument } = require('./frontend_instrumentation.cjs');
module.exports = function sourceCoverage(source) {
  const result = instrument(source, this.resourcePath);
  this.callback(null, result.code, result.map);
};
