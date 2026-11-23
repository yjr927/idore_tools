'use strict';

// Keep previously shared discovery section links working.
const discoverySections = new Set(['#candidates', '#collection-history', '#collection-status']);
location.replace(discoverySections.has(location.hash) ? './discovery.html' + location.hash : './gallery.html');
