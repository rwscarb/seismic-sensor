const { defineConfig } = require('cypress');

module.exports = defineConfig({
  e2e: {
    baseUrl: 'http://localhost:8099',
    video: false,
    screenshotOnRunFailure: true,
    defaultCommandTimeout: 8000,
    supportFile: false,
  },
});
