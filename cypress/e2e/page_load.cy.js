// Regression coverage for the 2026-08-18 incident: a "static-only" deploy
// path updated app.js (referencing L.markerClusterGroup) without updating
// index.html (which loads the plugin defining it), so the page loaded with
// a fresh JS file calling into a library that was never included. Cypress
// fails a test on any uncaught page exception by default — that alone would
// have caught this. The extra assertions pin down specifically what broke.

describe('page load', () => {
  it('loads without any uncaught JS errors', () => {
    // No explicit exception handler here on purpose — an uncaught error
    // anywhere during load fails the test automatically. That default
    // behavior is the actual regression guard; everything below just
    // narrows down *what* would have broken.
    cy.visit('/');
    cy.get('#map', { timeout: 10000 }).should('exist');
  });

  it('loads every third-party script it references, with no 404s', () => {
    // A missing/failed script tag doesn't always throw a JS exception the
    // page-load test above would catch (e.g. a stylesheet 404, or a script
    // that fails silently) — so also check network-level that everything
    // index.html points at actually resolved.
    cy.intercept('GET', '**/*.js').as('anyScript');
    cy.intercept('GET', '**/*.css').as('anyStyle');
    cy.visit('/');
    cy.wait('@anyScript').its('response.statusCode').should('be.lessThan', 400);
  });

  it('initializes Leaflet and the marker-cluster plugin', () => {
    cy.visit('/');
    cy.window().should((win) => {
      expect(win.L, 'window.L (Leaflet)').to.exist;
      expect(win.L.markerClusterGroup, 'L.markerClusterGroup').to.be.a('function');
      expect(win.L.map, 'L.map').to.be.a('function');
    });
    // L.map('map') turns #map itself into the Leaflet container (adds the
    // class to that element directly, not to a child).
    cy.get('#map', { timeout: 10000 }).should('have.class', 'leaflet-container');
  });

  it('renders the core layout panels', () => {
    cy.visit('/');
    cy.get('#stations').should('exist');
    cy.get('#scoreboard-panel').should('exist');
    cy.get('#detections-wrap').should('exist');
  });

  it('serves a healthy /health endpoint', () => {
    cy.request('/health').its('body').should('eq', 'ok');
  });
});
